# Ouroboros TUI — SuperLightTUI Edition

A fast, native TUI dashboard for [Ouroboros](https://github.com/Q00/ouroboros) workflow monitoring, built with [SuperLightTUI (SLT)](https://github.com/subinium/SuperLightTUI) — an immediate-mode Rust TUI library.

Alternative to the Python/Textual TUI. Single static binary, no Python runtime needed.

## Install

```bash
# From source
cargo install --path .

# Or build locally
cargo build --release
./target/release/ouroboros-tui
```

## Usage

```bash
# Monitor real Ouroboros workflows (reads ~/.ouroboros/ouroboros.db)
ouroboros-tui

# Same as above (matches `ouroboros tui monitor` interface)
ouroboros-tui monitor

# Custom DB path
ouroboros-tui --db-path /path/to/ouroboros.db

# Demo mode with mock data
ouroboros-tui --mock
```

Run this in a **separate terminal** while `ooo run` or `ooo evolve` is executing in Claude Code.

## Screens

| Key | Screen | Description |
|-----|--------|-------------|
| `1` | Dashboard | Phase bar, AC execution tree, node detail, activity |
| `2` | Execution | Timeline, phase outputs, tool calls, metrics |
| `3` / `l` | Logs | Filterable log viewer with level indicators |
| `4` / `d` | Debug | State dump, drift/cost metrics, raw event stream |
| `e` | Lineage | Lineage explorer with generation history |
| `s` | Sessions | Session selector (switch between workflows) |

## Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `1-4` | Switch screen |
| `l` | Logs |
| `d` | Debug |
| `e` | Lineage |
| `s` | Session selector |
| `p` / `r` | Pause / Resume |
| `Ctrl+P` | Command palette |
| `↑↓` | Navigate tree/list |
| `Enter` | Select |
| Mouse | Click to select |

## Comparison with Python TUI

| | Python (Textual) | Rust (SLT) |
|---|---|---|
| Startup | ~1s + Python runtime | <10ms, static binary |
| Binary | N/A (interpreted) | 2.7 MB |
| Dependencies | 84 pip packages | 2 crates (crossterm + unicode-width) |
| FPS | Event-driven | 60fps double-buffered |
| DB | Same `~/.ouroboros/ouroboros.db` | Same |

## Integration with Ouroboros CLI

To use this as the default TUI backend, add to your shell config:

```bash
alias ouroboros-monitor='ouroboros-tui'
```

Or with the `--backend` flag (requires Ouroboros PR):

```bash
ouroboros tui monitor --backend rust
```

## Theme

Uses Rose Pine color scheme. The palette adapts to terminal capabilities (true color, 256 color, 16 color).

## Project Structure

```
src/
├── main.rs               Entry point, theme, header/tabs/footer
├── state.rs              Application state types
├── db.rs                 SQLite EventStore reader
├── mock.rs               Demo data generator
└── views/
    ├── dashboard.rs      Phase bar + AC tree + detail
    ├── execution.rs      Timeline + phase outputs + tools
    ├── logs.rs           Filterable log viewer
    ├── debug.rs          State dump + event stream
    ├── lineage.rs        Lineage explorer
    └── session_selector.rs  Session picker
```

## License

MIT
