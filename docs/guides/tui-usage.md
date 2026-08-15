# TUI Dashboard Reference

> 한국어: [tui-usage.ko.md](./tui-usage.ko.md)

Ouroboros includes two terminal user interface (TUI) backends for real-time
workflow monitoring: the default Python UI built with
[Textual](https://textual.textualize.io/), and the native Rust UI built with
[SuperLightTUI](https://github.com/subinium/SuperLightTUI) (`slt`). Their data
model overlaps, but their screens and key bindings are not interchangeable.

> **New to Ouroboros?** See [Getting Started](../getting-started.md) for install and onboarding.

## Launching the TUI

```bash
# Default Python Textual backend
ouroboros tui monitor

# Monitor with a specific database file
ouroboros tui monitor --db-path ~/.ouroboros/ouroboros.db

# Native Rust SLT backend (requires the ouroboros-tui binary)
ouroboros tui monitor --backend slt

# Run the native backend's owned demo simulation directly
ouroboros-tui --mock
```

The default Textual backend opens with a **Session Selector** where you pick an
existing session, then switches to its Dashboard. SLT loads the most recent
session into its Dashboard and exposes its session list as screen `4`.

## Screen Overview

<!-- tui-contract:textual-screens -->
### Textual screens (default backend)

Textual provides four numbered screens plus separate Session Selector and
Lineage screens:

| Key | Shortcut | Screen | Purpose |
|-----|----------|--------|---------|
| `1` | | **Dashboard** | Primary view: phase progress, AC tree, node details |
| `2` | | **Execution** | Execution timeline, phase outputs, detailed events |
| `3` | `l` | **Logs** | Filterable log viewer with level-based coloring |
| `4` | `d` | **Debug** | State inspector, raw events, configuration dump |
| | `s` | **Session Selector** | Switch between sessions |
| | `e` | **Lineage** | View evolutionary lineage across generations |

<!-- tui-contract:slt-screens -->
### SLT screens (native backend)

SLT has a different four-screen set. It has no separate Logs or Debug screen;
`l` opens a log panel inside Execution and, when no modal owns the key, `Esc`
closes it. While the command palette is open, `Esc` closes the palette and
preserves the underlying log panel and filter. While the filter has focus, `l`
enters filter text instead of closing the panel. The global `q`, `1`-`4`, and
`Ctrl+P` shortcuts remain reserved while the panel is open.

| Key | Shortcut | Screen | Purpose |
|-----|----------|--------|---------|
| `1` | | **Dashboard** | Phase progress, AC tree, node details |
| `2` | | **Execution** | Phase outputs, event timeline, optional log panel |
| `3` | `e` | **Lineage** | Evolutionary generation history |
| `4` | `s` | **Sessions** | Browse and load sessions |
| | `l` | **Execution log panel** | Open only while Execution is active |
| | `Esc` | **Execution log panel** | Close the open panel when no modal is active |

## Textual Dashboard Screen (Key: 1)

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

## Textual Logs Screen (Key: 3 or `l`)

Filterable, scrollable log viewer with color-coded severity levels:

| Level | Color |
|-------|-------|
| DEBUG | Dim grey |
| INFO | White |
| WARNING | Yellow |
| ERROR | Red |
| CRITICAL | Bold red |

Logs update in real-time as the workflow executes.

## Textual Execution Screen (Key: 2)

Detailed execution information:
- **Timeline**: Chronological list of execution events
- **Phase outputs**: Results from each phase
- **Tool calls**: What tools the agent used and their results

## Textual Debug Screen (Key: 4 or `d`)

For troubleshooting:
- **State inspector**: Current `TUIState` values (phase, drift, cost, AC tree)
- **Raw events**: Unprocessed events from the EventStore
- **Configuration**: Active pipeline and execution config

## Textual Session Selector (Key: `s`)

Browse and select from available sessions. Useful when multiple workflows have been executed and you want to switch between them.

## Textual Lineage Screen (Key: `e`)

View evolutionary lineage across generations when using evolutionary loops (`ooo evolve`). Shows how seeds evolved and converged over multiple iterations.

## Keyboard Shortcuts

<!-- tui-contract:textual-keys -->
### Textual key resolution

| Key | Action |
|-----|--------|
| `1` - `4` | Switch to Textual screen 1-4 |
| `s` | Session Selector |
| `e` | Lineage view |
| `q` | Quit the TUI |
| `p` | Request pause when an execution owner is connected |
| `r` on Dashboard or Session Selector | Request resume when an execution owner is connected |
| `r` on Execution | Refresh the Execution view; it does **not** resume |
| `r` on Debug | Refresh the Debug view; it does **not** resume |
| `r` on Logs | No active binding; it does **not** resume |
| `r` on Lineage selector | Refresh the lineage list; it does **not** resume |
| `r` on Lineage detail | Open the rewind confirmation flow; it does **not** resume |

> **Note**: `ouroboros tui monitor` attaches to the event store as an observer
> and does not own the running execution, so `p` and `r` are hidden from the
> footer as lifecycle controls and do nothing there. Use
> `ouroboros cancel execution` to stop a run.
>
> In Textual, the lifecycle bindings appear only when an embedding caller
> connects an execution owner
> via `OuroborosTUI.set_pause_callback()` / `set_resume_callback()`. Even then,
> a screen-level `r` binding in the table above wins over the app-level resume
> binding. Dashboard and Session Selector expose resume.
>
> The displayed lifecycle status changes only after the execution control path
> persists an acknowledged lifecycle event — `orchestrator.session.paused` for a
> pause, and progress carrying `runtime_status: running` for a resume. A request
> that is unavailable or fails is reported as a warning/error and leaves the
> status unchanged.

<!-- tui-contract:slt-lifecycle -->
### SLT keys and demo ownership

| Key | Action |
|-----|--------|
| `1` - `4` | Switch to SLT screen 1-4 |
| `e` | Lineage screen (`3`) |
| `s` | Sessions screen (`4`) |
| `l` | Open the log panel while Execution (`2`) is active |
| `Esc` | Close the command palette when active; return from Sessions to Dashboard; close the open log panel in Execution |
| `Ctrl+P` | Open the command palette |
| `q` | Quit the TUI |
| `p` / `r` | Pause/resume only when SLT owns a demo simulation |

SLT owns a demo simulation, and therefore exposes `p` / `r`, in three cases:

1. it is started explicitly as `ouroboros-tui --mock`;
2. the selected database opens but contains no events; or
3. the database cannot be opened, so SLT falls back to demo data.

The last two are automatic fallbacks, not control over a real execution. When
SLT attaches to a database containing real events, it is an observer: lifecycle
controls are removed from the footer and command palette. Persisted progress can
still move a displayed paused run back to running, but that projection does not
give the observer control over the run.

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

The Textual backend subscribes to the `EventStore` via polling (0.5s interval).
Events are converted to Textual messages and dispatched to the active screen:

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

SLT reads the same SQLite event store directly into Rust `AppState`. Its tab
mapping, lifecycle ownership, and mock fallbacks are separate from the Textual
message pipeline.

### Runtime contract sources

These descriptions are tied to the checked-in runtime definitions:

- Textual app bindings: [`src/ouroboros/tui/app.py`](../../src/ouroboros/tui/app.py)
- Textual screen overrides: [`src/ouroboros/tui/screens/`](../../src/ouroboros/tui/screens/)
- SLT screen mapping and mock fallback: [`crates/ouroboros-tui/src/main.rs`](../../crates/ouroboros-tui/src/main.rs)
- SLT lifecycle capability state: [`crates/ouroboros-tui/src/state.rs`](../../crates/ouroboros-tui/src/state.rs)

## Troubleshooting

**TUI doesn't show any data**
- Ensure a workflow is running or an execution ID was provided
- Check the active EventStore path with `ouroboros config show`, then confirm that file exists.

**AC tree doesn't update**
- The TUI polls every 0.5s; brief delays are expected
- If the run is paused, resume it from the process that owns the execution;
  `ouroboros tui monitor` cannot resume a run

**Lifecycle pause/resume is unavailable**
- Expected in `ouroboros tui monitor` — it does not own the execution, so those
  lifecycle bindings are hidden. Screen-specific `r` actions such as refresh
  and rewind still work. Use `ouroboros cancel execution` to stop a run.

**Display issues**
- Ensure your terminal supports 256 colors and Unicode
- Minimum terminal size: 80 columns x 24 rows recommended
- Try a different terminal emulator if rendering is broken
