# CLI UX Redesign

Design rationale for the CLI command structure improvements in v0.8.0.

## Problem

The original CLI invocation for the most common operation was verbose:

```bash
ouroboros run workflow --orchestrator seed.yaml
```

This requires users to:
1. Know that `workflow` is a subcommand of `run` (even though it's the only real command)
2. Explicitly pass `--orchestrator` every time (even though it's the primary execution mode)
3. Type 5 tokens for the most basic operation

New users hitting `ouroboros run seed.yaml` would get a confusing "No such command 'seed.yaml'" error.

## Solution

### 1. Default Subcommand Routing

A custom `TyperGroup` subclass (`_DefaultWorkflowGroup`) intercepts argument parsing. When the first positional argument doesn't match a known subcommand, it prepends the default subcommand name.

```
ouroboros run seed.yaml
         ↓ (first arg "seed.yaml" is not a subcommand)
ouroboros run workflow seed.yaml
```

This pattern is applied to both `run` and `init`:

| Shorthand | Equivalent |
|-----------|-----------|
| `ouroboros run seed.yaml` | `ouroboros run workflow seed.yaml` |
| `ouroboros init "Build an API"` | `ouroboros init start "Build an API"` |

The routing is transparent: flags and options pass through correctly.

### 2. Orchestrator as Default

Orchestrator mode (Claude Agent SDK) is now the default for `run workflow`. The `--orchestrator` flag is replaced with `--orchestrator/--no-orchestrator` (default: True).

| Before | After |
|--------|-------|
| `ouroboros run workflow --orchestrator seed.yaml` | `ouroboros run seed.yaml` |
| `ouroboros run workflow seed.yaml` | `ouroboros run seed.yaml --no-orchestrator` |

### 3. Top-level Aliases

| Alias | Full Command |
|-------|-------------|
| `ouroboros monitor` | `ouroboros tui monitor` |

The `monitor` command is registered as a hidden command on the main app to keep `--help` output clean while still being discoverable.

## Before / After

### Before (v0.7.x)

```bash
# Common workflow
ouroboros init start "Build a REST API"
ouroboros run workflow --orchestrator seed.yaml
ouroboros tui monitor

# Resume
ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml
```

### After (v0.8.0)

```bash
# Common workflow (simplified)
ouroboros init "Build a REST API"
ouroboros run seed.yaml
ouroboros monitor

# Resume
ouroboros run seed.yaml --resume orch_abc123
```

## Backward Compatibility

All existing command paths continue to work:

| Old Command | Status |
|-------------|--------|
| `ouroboros run workflow seed.yaml` | Works (now defaults to orchestrator) |
| `ouroboros run workflow --orchestrator seed.yaml` | Works (explicit, same as default) |
| `ouroboros init start "context"` | Works |
| `ouroboros init list` | Works |
| `ouroboros tui monitor` | Works |
| `ouroboros run resume` | Works |

## Implementation

The core mechanism is a custom Click `Group` subclass that overrides `parse_args`:

```python
class _DefaultWorkflowGroup(typer.core.TyperGroup):
    default_cmd_name: str = "workflow"

    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = [self.default_cmd_name, *args]
        return super().parse_args(ctx, args)
```

The guard conditions prevent false matches:
- `args[0] not in self.commands` -- only activate when not a real subcommand
- `not args[0].startswith("-")` -- flags like `--help` pass through normally

## Files Changed

- `src/ouroboros/cli/main.py` -- Top-level `monitor` alias, updated help text
- `src/ouroboros/cli/commands/run.py` -- `_DefaultWorkflowGroup`, orchestrator default
- `src/ouroboros/cli/commands/init.py` -- `_DefaultStartGroup`
- `tests/unit/cli/test_main.py` -- 8 new shorthand tests
- `tests/e2e/test_cli_commands.py` -- Updated for new orchestrator default
- `docs/cli-reference.md` -- Updated documentation
