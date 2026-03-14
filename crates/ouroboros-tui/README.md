# ouroboros-tui

Native TUI monitor for [Ouroboros](https://github.com/Q00/ouroboros) workflows, built with [SuperLightTUI](https://github.com/subinium/SuperLightTUI).

Reads the same `~/.ouroboros/ouroboros.db` as the Python TUI. Run it in a separate terminal while `ooo run` or `ooo evolve` executes.

## Install

```bash
# From source (requires Rust toolchain)
cd crates/ouroboros-tui
cargo install --path .

# Via ouroboros CLI
ouroboros tui monitor --backend slt
```

## Usage

```bash
ouroboros-tui                           # default DB path
ouroboros-tui --db-path /path/to/db     # custom DB
ouroboros-tui --mock                    # demo mode
ouroboros-tui --help                    # show all options
```

## Screens

| Key | Shortcut | Screen |
|-----|----------|--------|
| `1` | | Dashboard — Double Diamond phase bar, AC tree, node detail |
| `2` | | Execution — Phase outputs, event timeline |
| `3` | `l` | Logs — Sortable/filterable table |
| `4` | `d` | Debug — State dump, drift/cost sparklines, events |
| `5` | `e` | Lineage — Evolutionary generation history |
| | `s` | Session Selector |

## Keys

`q` quit · `p`/`r` pause/resume · `1-5` screens · `Ctrl+P` command palette · `↑↓` navigate · `Enter` select · mouse click
