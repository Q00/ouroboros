# Deep Code Review: Hermes Runtime Integration

## Context

This changeset introduces **Hermes CLI** as a fourth runtime option alongside Claude, Codex, and OpenCode. It includes:
- New `HermesCliRuntime` class and Hermes artifact installer
- New `ouroboros.skills` package for runtime-agnostic skill packaging
- Tracked changes across config, setup, factory, adapter, and packaging
- Intentional decoupling: Codex/OpenCode setup no longer auto-registers with Claude MCP

The review covers correctness, packaging safety, regression risk, and test adequacy.

---

## Findings

### CRITICAL Severity

#### C1. `src/ouroboros/skills/__init__.py` shadows repo-root `skills/` — breaks ALL editable-install skill resolution (8 test failures)
- **Files**: `src/ouroboros/skills/__init__.py` (new) + `src/ouroboros/codex/artifacts.py:162-166` + `pyproject.toml`
- **Why**: The parent-directory walk in `_packaged_codex_skills_dir()` (lines 162-166) walks up from `codex/artifacts.py` and finds `src/ouroboros/skills/` before reaching the repo-root `skills/` directory. But `src/ouroboros/skills/` only contains `__init__.py` — no actual skill bundles. The walk stops too early, yielding a directory that has no `SKILL.md` files.
- **Root cause**: Adding `src/ouroboros/skills/__init__.py` created a Python package that shadows the repo-root skills directory in the parent walk. Previously, `src/ouroboros/skills/` didn't exist, so the walk continued up to the repo root where the actual skills live.
- **Impact**: **8 tests fail**, including Codex skill resolution, skill smoke tests, and passthrough tests. ALL skill interception is broken in editable (development) installs. Wheel installs work because `ouroboros/skills/` in the wheel contains both `__init__.py` and skill bundles via `force-include`.
- **Evidence**: `uv run pytest tests/` shows:
  - `FAILED test_codex_artifacts.py::test_resolves_repo_packaged_skill_path_by_default`
  - `FAILED test_codex_artifacts.py::test_installs_repo_packaged_skills_by_default`
  - `FAILED test_codex_artifacts.py::test_resolves_repo_skills_and_packaged_rules_by_default`
  - `FAILED test_codex_skill_smoke.py::test_packaged_ooo_prefixes_dispatch_from_skill_frontmatter[run]`
  - `FAILED test_codex_skill_smoke.py::test_packaged_ooo_prefixes_dispatch_from_skill_frontmatter[interview]`
  - `FAILED test_codex_cli_passthrough_smoke.py` (2 failures)
  - `FAILED test_codex_skill_fallback.py::test_codex_mcp_timeout_falls_back_to_pass_through_cli_flow`
- **Fix options**:
  1. Add `_contains_skill_bundles()` check to `_packaged_codex_skills_dir` parent walk so it skips directories without actual skill content
  2. Move the skill resolver out of `ouroboros.codex` into `ouroboros.skills.resolver` and use `importlib.resources.files("ouroboros.skills")` as primary path
  3. Remove `src/ouroboros/skills/__init__.py` and rely on implicit namespace packages for the force-include

### HIGH Severity

#### H1. No subprocess timeout protection in `execute_task`
- **File**: `src/ouroboros/orchestrator/hermes_runtime.py:252`
- **Why**: `await process.communicate()` blocks indefinitely. Codex/OpenCode have startup timeouts (60s) and idle timeouts (300s). A hung Hermes process freezes the entire orchestrator.
- **Evidence**: Line 252 — no timeout wrapper around `process.communicate()`.
- **Repro**: Start Hermes with a prompt that causes it to hang (e.g., waiting for interactive input). The orchestrator never recovers.

#### H2. No recursion depth tracking or environment isolation
- **File**: `src/ouroboros/orchestrator/hermes_runtime.py:245-250`
- **Why**: Codex/OpenCode track `_OUROBOROS_DEPTH` and strip Ouroboros env vars before spawning subprocesses. Hermes doesn't. If Hermes invokes Ouroboros via MCP → which invokes Hermes again → infinite recursion.
- **Evidence**: No `env=` parameter passed to `create_subprocess_exec()`. No depth counter. Compare with `codex_cli_runtime.py` which increments `_OUROBOROS_DEPTH` and caps at 5.

#### H3. `_packaged_codex_skills_dir` primary importlib resolution is dead — relies entirely on fragile fallback
- **File**: `src/ouroboros/codex/artifacts.py:155-169` + `pyproject.toml` diff
- **Why**: The `force-include` change moves skills from `ouroboros/codex/skills` to `ouroboros/skills` in the wheel. The primary `importlib.resources.files("ouroboros.codex").joinpath("skills")` no longer finds a `skills` subdirectory. In wheel installs, the parent-walk fallback happens to work by finding `ouroboros/skills/` two levels up. In editable installs, the same fallback hits the C1 shadowing bug.
- **Verified**: Wheel test passes (`resolve_packaged_codex_skill_path("run")` works from installed wheel). Editable install fails (see C1).

#### H4. Cross-runtime setup decoupling is a user-facing behavioral change
- **File**: `src/ouroboros/cli/commands/setup.py:333-335` (Codex) and `:668-671` (OpenCode)
- **Why**: `_ensure_claude_mcp_entry()` was removed from `_setup_codex()` and `_setup_opencode()`. Users who ran `ouroboros setup --runtime codex` expected it to also register with Claude MCP. This is now a silent regression for multi-runtime users.
- **Evidence**: Diff removes the `if (Path.home() / ".claude").is_dir(): _ensure_claude_mcp_entry()` block from both functions. Tests were updated to assert the new behavior.
- **Recommendation**: Document the change in release notes. Consider a migration hint in setup output.

### MEDIUM Severity

#### M1. `install_hermes_skills` copies entire source tree including `__init__.py`
- **File**: `src/ouroboros/hermes/artifacts.py:76-77`
- **Why**: `shutil.copytree(source_root, target_dir)` copies everything from the resolved skills directory. In wheel installs, `source_root` is `ouroboros/skills/` which includes `__init__.py` (a Python package marker). This file is meaningless in `~/.hermes/skills/autonomous-ai-agents/ouroboros/` and pollutes the Hermes skill namespace.
- **Contrast**: `install_codex_skills()` in `codex/artifacts.py` iterates individual skill directories and copies each one, avoiding non-skill files.

#### M2. Hermes runtime reuses Codex-named skill resolver — coupling concern
- **File**: `src/ouroboros/orchestrator/hermes_runtime.py:22`
- **Why**: `from ouroboros.codex import resolve_packaged_codex_skill_path` — the Hermes runtime depends on a Codex-specific module for its own skill resolution. If the Codex module's resolution strategy changes (it already has — see H3), Hermes is silently affected. The function name and module path mislead about which runtime is consuming it.

#### M3. `_parse_quiet_output` may truncate content if session_id appears mid-output
- **File**: `src/ouroboros/orchestrator/hermes_runtime.py:80-85`
- **Why**: `_HERMES_SESSION_ID_PATTERN.search(output)` finds the **first** match. Line 84 slices `output[:match.start()]` discarding everything after the session_id line. If Hermes emits a session_id line followed by additional content, that content is silently dropped.
- **Evidence**: The multiline flag `(?m)` allows `^` to match any line start, so `session_id: ...` could appear anywhere in output.

#### M4. `prune` parameter accepted but silently ignored
- **File**: `src/ouroboros/hermes/artifacts.py:59-61`
- **Why**: `install_hermes_skills(prune=True)` is called by `_install_hermes_artifacts()` in setup.py, but the function assigns `_ = prune` and never uses it. Since the function does `shutil.rmtree` + `copytree` (full replacement), `prune` is semantically irrelevant, but the API is misleading.

#### M5. Session ID regex only matches lowercase hex
- **File**: `src/ouroboros/orchestrator/hermes_runtime.py:44-46`
- **Why**: `[a-f0-9]+` excludes uppercase hex. If Hermes ever generates uppercase hex session IDs, parsing silently fails and session resume breaks without error.

#### M6. `SkillInterceptRequest.skill_path` is a stale reference after context manager exit
- **File**: `src/ouroboros/orchestrator/hermes_runtime.py:315-323`
- **Why**: `resolved_skill_path = Path(str(skill_md_path))` captures the path string inside the `with resolve_packaged_codex_skill_path(...)` context manager. After the `with` block exits, `importlib.resources` may clean up the temp file. The path stored in `SkillInterceptRequest` could point to a deleted temp file.
- **Current impact**: LOW — `skill_path` is not read after construction. But it's a latent bug.

### LOW Severity

#### L1. Missing `encoding="utf-8"` in `_setup_hermes` config I/O
- **File**: `src/ouroboros/cli/commands/setup.py:407,417`
- **Why**: `config_path.read_text()` and `config_path.open("w")` omit encoding. Other setup functions (including `_register_hermes_mcp_server`) specify `encoding="utf-8"`. Inconsistent and could fail on non-UTF-8 default locales.

#### L2. `stdout_data.decode()` uses system default encoding
- **File**: `src/ouroboros/orchestrator/hermes_runtime.py:253`
- **Why**: `.decode()` defaults to system encoding. Codex/OpenCode explicitly decode as UTF-8. Could fail if Hermes outputs UTF-8 on a system with a different default encoding.

#### L3. Hermes docs overstate functionality
- **File**: `docs/runtime-guides/hermes.md`
- **Why**: Documentation describes session management and quiet-output parsing as if they are robust features. The implementation lacks timeout protection (H1), depth tracking (H2), and streaming (Hermes waits for full output). The docs should mention these limitations.

#### L4. `.codex` is an empty untracked file
- **File**: `.codex`
- **Why**: Repository hygiene. Empty file with no apparent purpose. Should either be given content, added to `.gitignore`, or removed.

#### L5. `__pycache__` in `src/ouroboros/skills/`
- **Why**: Generated bytecode directory in the new skills package. Should be gitignored.

---

## Verification Steps to Execute

### Quality Gates (mandatory)

```bash
# 1. Sync dependencies
uv sync --dev

# 2. Linting
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# 3. Type checking
uv run mypy src/ouroboros

# 4. New Hermes tests
uv run pytest tests/unit/hermes/test_artifacts.py tests/unit/orchestrator/test_hermes_runtime.py -v

# 5. Blast-radius tests
uv run pytest tests/unit/cli/test_setup.py tests/unit/cli/test_config.py \
  tests/unit/orchestrator/test_runtime_factory.py tests/unit/providers/test_factory.py -v

# 6. Full orchestrator + CLI suites
uv run pytest tests/unit/orchestrator/ -v
uv run pytest tests/unit/cli/ -v

# 7. Full test suite with coverage
uv run pytest tests/ --cov=src/ouroboros --cov-report=term-missing -v
```

### Packaging Checks (mandatory — release blocker)

```bash
# 8. Build wheel
uv build

# 9. Inspect wheel contents
unzip -l dist/ouroboros_ai-*.whl | grep -E "(skills/|codex/skills)"

# 10. Verify importlib resolution from wheel
pip install dist/ouroboros_ai-*.whl --force-reinstall --no-deps -q && \
  python -c "
import importlib.resources
# Check new skills package
skills = importlib.resources.files('ouroboros.skills')
print('ouroboros.skills:', skills)
# Check old codex path
codex = importlib.resources.files('ouroboros.codex')
codex_skills = codex.joinpath('skills')
print('ouroboros.codex/skills exists:', codex_skills.is_dir())
# Check resolve_packaged_codex_skill_path still works
from ouroboros.codex.artifacts import resolve_packaged_codex_skill_path
with resolve_packaged_codex_skill_path('run') as p:
    print('resolve run skill:', p, p.is_file())
"
```

### Setup Smoke Test (temp HOME)

```bash
# 11. Hermes setup in isolated environment
export TMPDIR=$(mktemp -d)
export HOME=$TMPDIR
mkdir -p $HOME/.local/bin
# Create stub hermes binary
echo '#!/bin/sh' > $HOME/.local/bin/hermes && chmod +x $HOME/.local/bin/hermes
export PATH="$HOME/.local/bin:$PATH"

uv run ouroboros setup --runtime hermes

# Verify outcomes:
cat $HOME/.ouroboros/config.yaml   # runtime_backend=hermes, llm.backend preserved
cat $HOME/.hermes/config.yaml      # mcp_servers.ouroboros present
ls $HOME/.hermes/skills/autonomous-ai-agents/ouroboros/  # skills installed
test ! -f $HOME/.claude/claude_desktop_config.json  # Claude config NOT touched

# Cleanup
rm -rf $TMPDIR
```

### Regression Smoke: Codex/OpenCode setup decoupling

```bash
# 12. Verify Codex setup no longer touches Claude config
export TMPDIR=$(mktemp -d)
export HOME=$TMPDIR
mkdir -p $HOME/.local/bin $HOME/.claude
echo '#!/bin/sh' > $HOME/.local/bin/codex && chmod +x $HOME/.local/bin/codex
export PATH="$HOME/.local/bin:$PATH"

uv run ouroboros setup --runtime codex

# Should NOT have Claude MCP entry:
test ! -f $HOME/.claude/claude_desktop_config.json && echo "PASS: Claude config untouched"
rm -rf $TMPDIR
```

---

## Test Adequacy Assessment

### Covered
- Hermes runtime properties, skill intercept pattern matching, template resolution
- Quiet-output parsing (banner stripping, reasoning stripping, session ID extraction)
- Interview session resume via handle metadata
- Subprocess execution with mocked process (success + error paths)
- Hermes artifact installation (editable-install path)
- Setup config writing, MCP registration, scalar config repair
- Hermes not registered as LLM backend
- Runtime factory routing for hermes aliases

### Missing Coverage (recommend adding before merge)
1. **Wheel-install skill resolution** — No test verifies `resolve_packaged_codex_skill_path` works after the `force-include` path change in a non-editable install
2. **`install.sh` behavior** — Shell script changes are untested (no integration test harness)
3. **Malformed Hermes quiet output** — No tests for: empty stdout, binary garbage, session_id appearing mid-output, extremely large output
4. **Partial/missing session metadata** — No test for `handle.metadata` missing `_INTERVIEW_SESSION_METADATA_KEY` when `current_handle` is non-None
5. **Broken YAML frontmatter** — Tests don't cover: truncated YAML, non-UTF-8 bytes, YAML with anchors/aliases
6. **Bad `~/.hermes/config.yaml` shapes** — Tests cover scalar top-level but not: nested non-dict `mcp_servers`, missing permissions, read-only file
7. **Regression coverage for Claude/Codex/OpenCode** — Existing tests were updated to match new behavior but no explicit regression test verifies Codex skill resolution still works after the packaging path change
8. **Timeout/hang scenarios** — No test for Hermes subprocess that never exits
9. **Depth recursion** — No test for Ouroboros→Hermes→Ouroboros recursion prevention

---

## Architectural Recommendations (non-blocking)

### R1. Backend-agnostic shared skill resolver
The codebase should move `resolve_packaged_codex_skill_path` and the skill resolution chain out of `ouroboros.codex` into a shared module (e.g., `ouroboros.skills.resolver`). Currently Hermes imports from `ouroboros.codex` and the Codex resolver's `importlib` path is broken by the packaging change. A unified resolver using `importlib.resources.files("ouroboros.skills")` would be the single source of truth.

### R2. Common subprocess runtime base class
`HermesCliRuntime`, `CodexCliRuntime`, and `OpenCodeRuntime` share: CLI path resolution, skill interception, session resume, template substitution, MCP handler loading, and interview session tracking. Extracting a `SubprocessAgentRuntime` base class would eliminate ~300 lines of duplicated logic across the three runtimes.

### R3. Declarative runtime registry
The current pattern of adding `if/elif` branches in `runtime_factory.py`, `setup.py`, `config.py`, and `models.py` for each new runtime doesn't scale. A registry pattern (decorator or dict-based) where each runtime self-registers its factory, setup handler, and config schema would reduce the blast radius of adding future runtimes.

### R4. Unified installer/config-writer with safer migration
`_setup_hermes`, `_setup_codex`, `_setup_opencode`, and `_setup_claude` share the same structure: read config → merge → write. Each has slightly different error handling and encoding practices (see L1). A shared `ConfigWriter` with safe-write (temp file + rename) and consistent encoding would reduce inconsistency.

### R5. Packaging/setup smoke-test harness
The packaging change (H3) and setup decoupling (H4) demonstrate the need for an automated smoke-test that: builds the wheel → installs in a venv → verifies skill resolution → runs setup in a temp HOME → checks file-system side effects. This would catch packaging regressions before they reach CI unit tests (which run from source checkout).

---

## Verified Test Results

```
Quality Gates:
  ruff check:   PASS
  ruff format:  FAIL — 4 files need reformatting
  mypy:         PASS — no issues in 244 source files
  pytest:       8 FAILED, 4519 passed, 2 skipped (75% coverage)

Packaging:
  uv build:     PASS — wheel builds successfully
  Wheel skills: 19 skill bundles at ouroboros/skills/ (correct)
  Wheel codex:  ouroboros/codex/skills/ ABSENT (expected after change)
  importlib (wheel): resolve_packaged_codex_skill_path("run") WORKS via fallback
  importlib (editable): resolve_packaged_codex_skill_path("run") FAILS (C1 bug)

Setup Smoke (temp HOME):
  Hermes setup:        PASS — config, MCP, skills all correct
  Hermes Claude leak:  PASS — no .claude dir created
  Codex Claude decoup: PASS — Claude config untouched
```

## Summary for Merge Decision

**Merge blocker**: C1 — `src/ouroboros/skills/__init__.py` shadows repo-root `skills/`, breaking ALL skill resolution in editable installs (8 test failures). This must be fixed before merge.

**Must fix**: H1 (no timeout), H2 (no depth guard).

**Should fix**: 4 ruff format violations.

**User-facing change requiring docs**: H4 (Codex/OpenCode no longer auto-register with Claude).

**Safe to merge after**: Fixing C1 (skill shadowing), H1 (timeout), H2 (depth guard), formatting, and documenting H4.
