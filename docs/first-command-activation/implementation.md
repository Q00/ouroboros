# First-command activation implementation

> Completed: 2026-08-13
> Branch: `main` worktree

> Historical note: the v2 lean telemetry contract removed the PostHog
> onboarding-surface dimension and its installer hint.

## Summary

Added privacy-safe onboarding-surface attribution and clarified the first-run
commands across the primary onboarding surfaces.

## Files modified

| File | Change |
|---|---|
| `scripts/install.sh` | Maps README and Getting Started install refs and persists a restrictive local hint without relabeling an existing first surface. |
| `src/ouroboros/telemetry.py` | Adds the fixed enum detector and MCP event allowlist fields; preserves hints after setup. |
| `src/ouroboros/config/untrusted_env.py` | Blocks project `.env` from overriding the attribution field. |
| `TELEMETRY.md` | Documents the exact event contract and enum behavior. |
| `tests/unit/test_telemetry.py` | Covers exact keys, enum validation, setup fallback, and hint preservation. |
| `tests/unit/scripts/test_install_runtime_selection.py` | Covers installer mapping and protection against relabeling on reinstall. |
| `docs/getting-started.md` | Makes setup → first interview copyable for Claude, standalone CLI, and Codex. |
| `README.md`, `README.ko.md`, `README.zh-CN.md` | Aligns the first command and Codex plugin instructions. |
| `docs/runtime-guides/codex.md` | Adds plugin and standalone first-command paths. |
| `docs/runtime-guides/opencode.md` | Adds setup-before-first-interview path. |

## Runtime behavior

Resolution order:

1. Explicit fixed enum from the trusted process environment.
2. Installer hint at `~/.ouroboros/first_command_surface`.
3. Existing `~/.ouroboros/config.yaml` → `setup_complete`.
4. `unknown`.

The project `.env` loader denylist includes
`OUROBOROS_FIRST_COMMAND_SURFACE`, so a checked-out repository cannot silently
relabel an activation cohort.

## Verification

```text
uv run pytest tests/unit/test_telemetry.py \
  tests/unit/scripts/test_install_runtime_selection.py \
  tests/unit/test_install_ref_docs_contract.py \
  tests/unit/config/test_loader_env.py -q
414 passed in 165.97s
```

This verifies the implementation, installer mapping, documentation ref
contract, and project `.env` trust boundary. Additional checks still required
before calling the backlog item complete:

- Run the full relevant product test suite after the documentation patch.
- Expose the changed installer/docs on the release branch.
- Re-run `./ops/posthog.sh activation` after seven complete production days and
  compare the ordered MCP-start cohort against the 482 → 127 baseline.
