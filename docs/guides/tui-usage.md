# TUI Dashboard Reference

Ouroboros includes an interactive terminal user interface (TUI) built with [Textual](https://textual.textualize.io/) for real-time workflow monitoring.

> **New to Ouroboros?** See [Getting Started](../getting-started.md) for install and onboarding.

## Launching the TUI

```bash
ouroboros tui monitor

# Monitor with a specific database file
ouroboros tui monitor --db-path ~/.ouroboros/ouroboros.db
```

When launched, the TUI opens with a **Session Selector** screen where you pick an existing session to monitor. Once selected, it switches to the Dashboard.

## Screen Overview

The TUI provides 4 main screens, switchable via number keys or letter shortcuts:

| Key | Shortcut | Screen | Purpose |
|-----|----------|--------|---------|
| `1` | | **Dashboard** | Primary view: phase progress, AC tree, node details |
| `2` | | **Execution** | Execution timeline, phase outputs, detailed events |
| `3` | `l` | **Logs** | Filterable log viewer with level-based coloring |
| `4` | `d` | **Debug** | State inspector, raw events, configuration dump |
| | `s` | **Session Selector** | Switch between sessions |
| | `e` | **Lineage** | View evolutionary lineage across generations |

## Dashboard Screen (Key: 1)

The dashboard is the primary monitoring view with three sections:

```
+---------------------------------------------------------------------+
|  < Discover  ->  * Define  ->  < Design  ->  > Deliver              |
+----------------------------------+----------------------------------+
|                                  |                                  |
|  AC EXECUTION TREE               |  NODE DETAIL                     |
|  +- root                         |                                  |
|    +- ◐ AC1 (executing)          |  AC: AC1                         |
|    | +- ● SubAC1 (complete)      |  Status: Executing               |
|    | +- ○ SubAC2 (pending)       |  Depth: 1                        |
|    +- ○ AC2 (pending)            |                                  |
|    +- ● AC3 (complete)           |  Content:                        |
|                                  |  Create a User model with...     |
|                                  |                                  |
+----------------------------------+----------------------------------+
```

### Double Diamond Phase Bar

Shows current position in the four-phase execution cycle:

- **Discover** -- diverging to explore the problem space
- **Define** -- converging on the core problem
- **Design** -- diverging to explore solutions
- **Deliver** -- converging on implementation

The active phase is highlighted. Phases progress automatically as the workflow advances.

### AC Execution Tree

Hierarchical view of all acceptance criteria and their sub-ACs:

| Icon | Status |
|------|--------|
| `○` (dim) | Pending -- not yet started |
| `⊘` (red) | Blocked -- waiting on dependency |
| `◐` (yellow) | Executing -- currently running |
| `●` (green) | Completed -- passed evaluation |
| `✖` (red) | Failed -- did not pass |
| `◆` (blue) | Atomic -- leaf node, no further decomposition |
| `◇` (cyan) | Decomposed -- has child sub-ACs |

**Navigation**: Use arrow keys to move through the tree. Press Enter or click to select a node and view its details in the right panel. Press `t` to focus the tree widget.

### Node Detail Panel

When an AC or sub-AC is selected in the tree, this panel shows:
- **ID**: Node identifier
- **Status**: Current execution status
- **Depth**: Tree depth (0 = root, 1 = top-level AC, 2+ = sub-AC)
- **Content**: The full acceptance criterion text

## Logs Screen (Key: 3 or `l`)

Filterable, scrollable log viewer with color-coded severity levels:

| Level | Color |
|-------|-------|
| DEBUG | Dim grey |
| INFO | White |
| WARNING | Yellow |
| ERROR | Red |
| CRITICAL | Bold red |

Logs update in real-time as the workflow executes.

## Execution Screen (Key: 2)

Detailed execution information:
- **Timeline**: Chronological list of execution events
- **Phase outputs**: Results from each phase
- **Tool calls**: What tools the agent used and their results

## Debug Screen (Key: 4 or `d`)

For troubleshooting:
- **State inspector**: Current `TUIState` values (phase, drift, cost, AC tree)
- **Raw events**: Unprocessed events from the EventStore
- **Configuration**: Active pipeline and execution config

## Session Selector (Key: `s`)

Browse and select from available sessions. Useful when multiple workflows have been executed and you want to switch between them.

## Lineage Screen (Key: `e`)

View evolutionary lineage across generations when using `ouroboros evolve`. Shows how seeds evolved and converged over multiple iterations.

## Keyboard Shortcuts

### Global

| Key | Action |
|-----|--------|
| `1` - `4` | Switch to screen 1-4 |
| `s` | Session Selector |
| `e` | Lineage view |
| `q` | Quit the TUI |
| `r` | Resume execution |
| `p` | Pause execution |

### Navigation

| Key | Action |
|-----|--------|
| `Up` / `Down` | Move selection / scroll |
| `Tab` | Focus next widget |
| `Shift+Tab` | Focus previous widget |
| `Enter` | Select / expand |

### Dashboard Specific

| Key | Action |
|-----|--------|
| `t` | Focus AC tree widget |
| `Up` / `Down` | Navigate AC tree |
| `Enter` | Select AC node for detail view |

## Architecture Notes

The TUI subscribes to the `EventStore` via polling (0.5s interval). Events are converted to Textual messages and dispatched to the active screen:

```
EventStore -> app._subscribe_to_events() (poll 0.5s)
           -> create_message_from_event()
           -> post_message() -> screen handlers
```

Key message types:
- `PhaseChanged` -- Double Diamond phase transition
- `ACUpdated` -- AC status change
- `WorkflowProgressUpdated` -- AC tree structure + status
- `ExecutionUpdated` -- session started/completed/failed/paused
- `SubtaskUpdated` -- sub-task hierarchy updates
- `DriftUpdated` -- drift score change
- `CostUpdated` -- token usage / cost update
- `ToolCallStarted` / `ToolCallCompleted` -- agent tool usage
- `AgentThinkingUpdated` -- agent reasoning output
- `ParallelBatchStarted` / `ParallelBatchCompleted` -- parallel execution events

## Troubleshooting

**TUI doesn't show any data**
- Ensure a workflow is running or an execution ID was provided
- Check that the EventStore database exists: `ls ~/.ouroboros/ouroboros.db`

**AC tree doesn't update**
- The TUI polls every 0.5s; brief delays are expected
- Press `r` to resume execution if paused

**Display issues**
- Ensure your terminal supports 256 colors and Unicode
- Minimum terminal size: 80 columns x 24 rows recommended
- Try a different terminal emulator if rendering is broken
