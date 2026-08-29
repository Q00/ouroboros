# Testing Guide

This page describes the current test topology, isolation contract, and commands
used by CI. The workflow files are executable authority; this page explains how
to reproduce them locally.

## Test Topology

| Suite | Location | Purpose |
|---|---|---|
| Unit | `tests/unit/` | Isolated domain, adapter, CLI, MCP, persistence and UI contracts |
| Integration | `tests/integration/` | Cross-component behavior and real package/runtime boundaries |
| End to end | `tests/e2e/` | User-visible CLI and workflow scenarios |
| Conformance | `tests/conformance/` | Versioned protocol and Workflow IR contracts |
| Canonical | `tests/canonical/` | Opt-in product-reality scenarios such as `cli-todo` |
| Fixtures | `tests/fixtures/` | Shared package, config and protocol inputs |

Unit MCP tests are hermetic and do not require a pre-existing external service.
Some deliberately create loopback HTTP servers or subprocesses inside the
isolated test environment. Broader server, package and multi-process behavior
lives under `tests/integration/mcp/`.

## Hermetic Home and Process Isolation

`tests/conftest.py` redirects `$HOME` before collection. This matters because
config, default EventStore paths, logs, worktrees, module-level constants and
spawned subprocesses can otherwise reach the developer's real
`~/.ouroboros` state.

Isolation has two levels:

1. A session-wide temporary home is installed before test modules import.
2. An autouse fixture gives each test a separate home.

Additional session fixtures give each xdist worker private heartbeat and
cancellation directories. Tests that need a specific home may override `$HOME`
inside the test after the autouse fixture runs.

Do not remove this chokepoint to fix one test. A test that requires real user
state must receive that state explicitly through a fixture or a subprocess
environment.

## Prepare an Environment

The pull-request test profile uses Python 3.12 with MCP and LiteLLM test groups:

```bash
uv sync --python 3.12 --dev --group mcp-test --group litellm-test
```

Python 3.14 intentionally omits LiteLLM, which does not support that interpreter
profile. See [Platform Support](../platform-support.md) for the package matrix.

## Fast Iteration

Run the narrowest owning suite first:

```bash
uv run --python 3.12 --no-sync pytest tests/unit/<area> -q
uv run --python 3.12 --no-sync pytest tests/unit/<file>.py::test_name -q
```

Examples:

```bash
uv run --python 3.12 --no-sync pytest tests/unit/orchestrator -q
uv run --python 3.12 --no-sync pytest tests/unit/mcp -q
uv run --python 3.12 --no-sync pytest tests/unit/cli/test_setup.py -q
```

For a broad local correctness pass matching `.github/workflows/test.yml`:

```bash
uv run --python 3.12 --no-sync pytest tests/ \
  -n 4 --dist worksteal --durations=25 -m "not performance" -q
```

Performance tests run separately and serially:

```bash
uv run --python 3.12 --no-sync pytest tests/ \
  -m performance -n 0 --durations=10 -q
```

## What CI Runs

`.github/workflows/test.yml` is authoritative:

- Pull requests run the correctness suite on Python 3.12.
- Pushes to `main` and `develop` run Python 3.12, 3.13 and 3.14.
- Python 3.12 on a push also emits coverage.
- Performance budgets run on pushes as an advisory serial job.
- The isolated Claude SDK/MCP 1 profile has its own compatibility job.

Lint, type checking, TypeScript bridge checks, native TUI checks and conditional
repository gates are described in [CI Gates and Branch Protection](./ci-gates.md).

## Test Design

### Assert behavior at the owning boundary

- Domain tests assert typed values and state transitions.
- Adapter tests assert the provider/runtime contract, including timeout and
  cleanup behavior.
- Persistence tests assert idempotency, interruption recovery and cold replay.
- CLI/MCP tests assert the public result and exit/error contract.
- UI reducer tests assert event folding; a visual or interactive change also
  needs a real rendered surface check.

Do not derive expected values from the output under test. Do not use a fallback
value equal to the override whose precedence you intend to prove.

### Async tests

`pyproject.toml` sets `asyncio_mode = "auto"` and function-scoped event loops.
Write an async test as an `async def`; use bounded awaits and signals rather than
fixed sleeps.

### Parallel safety

The default CI correctness suite uses xdist. Any process-external mutable
resource must be namespaced per test run or per worker:

- use temporary directories instead of fixed paths;
- bind port `0` instead of a fixed port;
- use unique database/container names;
- restore patched environment and module globals;
- never rely on the developer's real home or global git config.

A test that passes alone but fails under `-n 4 --dist worksteal` has an isolation
defect until proven otherwise.

### External and native smokes

Opt-in tests that require an installed runtime use explicit environment guards.
They are evidence in addition to, not a replacement for, deterministic contract
tests. Record the runtime version and command in the PR.

## Before Opening a PR

Run the required checks and any conditional gate for the changed paths:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run mypy src/ouroboros
uv run --python 3.12 --no-sync pytest tests/ \
  -n 4 --dist worksteal --durations=25 -m "not performance" -q
(cd src/ouroboros/opencode/plugin && bun install && bunx tsc --noEmit && bun test)
```

Then exercise the actual surface: invoke the CLI command, call the MCP tool,
run the runtime smoke, or render the UI. A green helper test does not prove a
user-facing workflow.

There is no standing list of known failures. If the baseline fails, reproduce
it on the exact base SHA and report the evidence rather than declaring it
pre-existing from memory.
