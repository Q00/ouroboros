# Ouroboros TUI Color System Specification

## Problem

Everything uses the same 2-3 colors (mostly `dim` and `accent`), making it impossible to scan information quickly. Labels, values, IDs, and metrics all blur together.

## Available Rose Pine Palette (from `ouroboros_theme()`)

| Theme Field     | Color                  | Role Name      | Hex Approx |
|-----------------|------------------------|----------------|------------|
| `primary`       | Rgb(196, 167, 231)     | iris/purple    | #C4A7E7    |
| `secondary`     | Rgb(49, 116, 143)      | pine/teal      | #31748F    |
| `accent`        | Rgb(246, 193, 119)     | gold           | #F6C177    |
| `text`          | Rgb(224, 222, 244)     | bright white   | #E0DEF4    |
| `text_dim`      | Rgb(110, 106, 134)     | muted          | #6E6A86    |
| `success`       | Rgb(156, 207, 216)     | foam/cyan      | #9CCFD8    |
| `warning`       | Rgb(246, 193, 119)     | gold           | #F6C177    |
| `error`         | Rgb(235, 111, 146)     | love/red       | #EB6F92    |
| `surface`       | Rgb(31, 29, 46)        | dark surface   | #1F1D2E    |
| `surface_hover` | Rgb(38, 35, 58)        | lighter surface| #26233A    |
| `surface_text`  | Rgb(144, 140, 170)     | subtle         | #908CAA    |

### Semantic Status Colors (raw Color::*)

| Color          | Usage                     |
|----------------|---------------------------|
| `Color::Green` | Completed, pass, positive |
| `Color::Yellow`| Running, in-progress, active |
| `Color::Red`   | Failed, blocked, negative |
| `Color::Cyan`  | Informational alternative |

---

## Core Design Principles

1. **Labels are ALWAYS `text_dim`** -- they are structural, not informational
2. **Values are ALWAYS colored** -- never dim, always a semantic color
3. **Most important info = `text` (bright white) + `.bold()`** -- goal text, content, summaries
4. **IDs = `accent` (gold)** -- universally recognizable as identifiers
5. **Metrics/counts = `success` (cyan)** for positive, `error` (red) for negative
6. **Active/running = `Color::Yellow` + `.bold()`** -- urgency stands out
7. **Section separators must be visible** -- use `ui.separator()` or `dim` horizontal rules

---

## 1. Dashboard: Detail Panel (`views/dashboard.rs` :: `render_detail`)

### Node Header Row
| Element            | Color                    | Style    | Theme Field / Code          |
|--------------------|--------------------------|----------|-----------------------------|
| Status icon        | Status-specific          | --       | `ACStatus` match (see below)|
| Node ID            | gold                     | bold     | `accent` + `.bold()`        |
| Status label       | Status-specific          | bold     | Status match + `.bold()`    |

### Status Color Map (used everywhere)
```
ACStatus::Completed  => Color::Green
ACStatus::Executing  => Color::Yellow
ACStatus::Failed     => Color::Red
ACStatus::Blocked    => Color::Red
ACStatus::Pending    => text_dim
_                    => Color::Cyan
```

### Content Section
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Content text       | bright white             | (wrap)   | `text`                    |

### Metadata Row
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "depth" label      | muted                    | --       | `text_dim`                |
| Depth value        | teal                     | --       | `secondary`               |
| "atomic"/"composite" | cyan / teal            | --       | `success` / `secondary`   |
| Sub-AC progress    | progress-color           | --       | Green/Yellow/dim by ratio |

### Active Tool (urgent -- visually distinct)
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "RUNNING" badge    | yellow                   | bold     | `Color::Yellow` + `.bold()`|
| Tool name          | gold                     | bold     | `accent` + `.bold()`      |
| Tool detail        | bright white             | --       | `text`                    |

### Thinking Section
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "Thinking" label   | muted                    | bold     | `text_dim` + `.bold()`    |
| Thinking text      | gold                     | italic   | `warning` + `.italic()`   |

### Tool History
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "Recent Tools" label | muted                  | bold     | `text_dim` + `.bold()`    |
| Tool count         | muted                    | --       | `text_dim`                |
| Success marker (check) | green                | --       | `Color::Green`            |
| Failure marker (x) | red                      | --       | `Color::Red`              |
| Tool name          | gold                     | --       | `accent`                  |
| Tool detail        | bright white             | --       | `text`                    |
| Duration           | muted                    | --       | `text_dim`                |
| "+N more" overflow | muted                    | --       | `text_dim`                |

### Live Activity Bar (bottom of dashboard)
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "LIVE" badge       | yellow                   | bold     | `Color::Yellow` + `.bold()`|
| AC short ID        | gold                     | --       | `accent`                  |
| Tool name          | bright white             | --       | `text`                    |
| Tool detail        | muted                    | --       | `text_dim`                |
| Separator (pipe)   | muted                    | --       | `text_dim`                |
| Empty state italic | muted                    | italic   | `text_dim` + `.italic()`  |

---

## 2. Session Selector (`views/session_selector.rs`)

### Table Columns (via slt::TableState -- color applied at row build time in `main.rs`)
| Column         | Color                    | Style    | Theme Field               |
|----------------|--------------------------|----------|---------------------------|
| Status icon    | Status-color (see below) | --       | Per-status                |
| Goal           | bright white             | bold     | `text` + bold (via markup)|
| ID (..xxxx)    | muted                    | --       | `text_dim`                |
| Timestamp      | teal                     | --       | `secondary`               |
| Event count    | gold                     | --       | `accent`                  |

#### Session Status Icon Colors
```
"done"      => Color::Green   (check)
"failed"    => Color::Red     (x)
"running"   => Color::Yellow  (play)
"paused"    => accent/gold    (pause)
"cancelled" => text_dim       (circle-slash)
```

### Footer
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Page info          | bright white             | --       | `text`                    |
| Help text          | muted                    | --       | `text_dim`                |
| Selected session ID| teal                     | --       | `secondary`               |
| Event count        | cyan                     | --       | `success`                 |

---

## 3. Lineage View (`views/lineage.rs`)

### Info Panel
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Labels (ID, Goal..)| muted                   | --       | `text_dim`                |
| Lineage ID         | teal                     | bold     | `secondary` + `.bold()`   |
| Goal text          | bright white             | --       | `text`                    |
| "Converged"        | green                    | bold     | `Color::Green` + `.bold()`|
| "In progress"      | yellow                   | --       | `Color::Yellow`           |
| Gen count value    | gold                     | --       | `accent`                  |
| Best score         | score-color              | --       | Green/Yellow/Red by threshold |

### Generation Rows
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Gen number (#N)    | purple                   | bold     | `primary` + `.bold()`     |
| Status icon+label  | status-color             | --       | Green/Yellow/Red/dim      |
| Score percentage   | score-color              | --       | Green>=90, Yellow>=70, Red|
| AC pass fraction   | progress-color           | --       | Green if complete, `accent`|
| Summary text       | bright white             | --       | `text`                    |
| "phase:" label     | muted                    | --       | `text_dim`                |
| Phase value        | teal                     | --       | `secondary`               |

### Summary Footer
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Total count        | muted                    | --       | `text_dim`                |
| Passed count       | cyan                     | --       | `success`                 |
| Failed count       | red                      | --       | `Color::Red`              |

---

## 4. Execution View (`views/execution.rs`)

### Phase Outputs
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Done phase icon (filled) | cyan               | --       | `success`                 |
| Active phase icon  | gold                     | --       | `accent`                  |
| Future phase icon  | muted                    | --       | `text_dim`                |
| Done label         | purple                   | --       | `primary`                 |
| Active label       | gold                     | bold     | `accent` + `.bold()`      |
| "Complete" text    | cyan                     | --       | `success`                 |
| "Active" text      | gold                     | --       | `accent`                  |
| Bullet points      | muted                    | --       | `text_dim`                |
| Output text        | bright white             | --       | `text`                    |

### Metrics Bar
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "Drift" label      | muted                    | --       | `text_dim`                |
| Drift value        | drift-color              | --       | Green/Yellow/Red          |
| "Cost" label       | muted                    | --       | `text_dim`                |
| Cost value         | cyan                     | --       | `success`                 |
| "Iter" label       | muted                    | --       | `text_dim`                |
| Iter value         | purple                   | --       | `primary`                 |

### Event Timeline
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Event count        | bright white             | bold     | `text` + `.bold()`        |
| "events" label     | muted                    | --       | `text_dim`                |
| Active tool dot    | gold                     | --       | `accent`                  |
| Active tool name   | bright white             | --       | `text`                    |
| Timestamp          | muted                    | --       | `text_dim`                |
| Event type         | type-color               | bold     | (see event_visual map)    |
| Event detail       | muted                    | --       | `text_dim`                |

---

## 5. Debug View (`views/debug.rs`)

### Left Panels (Execution, Drift, Cost)

All left-panel sections follow the same pattern:
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Labels (10-char padded) | muted               | --       | `text_dim`                |
| Exec/Session ID    | teal                     | --       | `secondary`               |
| Status             | status-color             | bold     | Per-status + `.bold()`    |
| Phase              | purple                   | bold     | `primary` + `.bold()`     |
| Iteration          | gold                     | --       | `accent`                  |
| Paused "Yes"       | gold                     | bold     | `accent` + `.bold()`      |
| Paused "No"        | muted                    | --       | `text_dim`                |
| Drift values       | drift-color              | --       | Green/Yellow/Red          |
| Token count        | gold                     | --       | `accent`                  |
| Cost USD           | cyan                     | --       | `success`                 |
| Active tool count  | purple                   | --       | `primary`                 |

### Right Panel (Event Stream)
| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Event count        | bright white             | bold     | `text` + `.bold()`        |
| "events" label     | muted                    | --       | `text_dim`                |
| "latest:" label    | muted                    | --       | `text_dim`                |
| Latest event type  | teal                     | --       | `secondary`               |
| Row timestamp      | muted                    | --       | `text_dim`                |
| Event icon         | type-color               | --       | (event_type_color map)    |
| Event type name    | type-color               | bold     | + `.bold()`               |
| Aggregate ID short | muted                    | --       | `text_dim`                |
| Data preview       | subtle (surface_text)    | --       | `surface_text`            |

**CHANGE**: Data previews currently use `dim` which is the same as labels. Use `surface_text` (Rgb(144, 140, 170)) to differentiate -- it is lighter than `text_dim` but clearly not "value" colored.

---

## 6. Logs View (`views/logs.rs`)

Logs use `slt::TableState` which handles its own column coloring. The filter bar:

| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "Filter" label     | muted                    | --       | `text_dim`                |
| Active filter btn  | gold                     | bold     | `accent` + `.bold()`      |
| Inactive filter btn| muted                    | --       | `text_dim`                |
| Row count          | muted                    | --       | `text_dim`                |
| Page info          | bright white             | --       | `text`                    |
| Help text          | muted                    | --       | `text_dim`                |

---

## 7. Header (`main.rs` :: `render_header`)

| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| "OUROBOROS" logo   | gold                     | bold     | `accent` + `.bold()`      |
| Status badge       | status-color             | bold     | Per-status + `.bold()`    |
| AC progress [n/m]  | bright white             | bold     | `text` + `.bold()`        |
| Elapsed time       | muted                    | --       | `text_dim`                |
| Cost ($)           | cyan                     | --       | `success`                 |
| Token count        | muted                    | --       | `text_dim`                |
| Iteration          | muted                    | --       | `text_dim`                |
| "Goal" label       | muted                    | --       | `text_dim`                |
| Goal text          | bright white             | bold     | `text` + `.bold()`        |
| Session ID short   | teal                     | --       | `secondary`               |

---

## 8. Tab Bar (`main.rs` :: `render_tab_bar`)

| Element            | Color                    | Style    | Theme Field               |
|--------------------|--------------------------|----------|---------------------------|
| Active tab key     | gold                     | bold     | `accent` + `.bold()`      |
| Active tab label   | bright white             | bold     | `text` + `.bold()`        |
| Inactive tab key   | muted                    | --       | `text_dim`                |
| Inactive tab label | muted                    | --       | `text_dim`                |
| Drift label        | muted                    | --       | `text_dim`                |
| Drift value        | drift-color              | --       | Green/Yellow/Red          |

---

## Summary of Key Changes Required

### Dashboard Detail Panel (most impactful)
1. **Active tool**: Change tool detail from `Color::Yellow` to `text` (white) -- differentiate name vs detail
2. **Live bar**: Change tool name from `dim` to `text` (white), tool detail stays `dim`
3. **Metadata row**: Add `secondary` for depth VALUE (currently whole string is dim)
   - Split: `"depth "` in `dim` + `format!("{depth}")` in `secondary`

### Session Selector
4. **Table rows are plain strings** -- slt::TableState renders them uniformly. To add per-column color, rows must embed styled segments OR the table must be rebuilt with manual rendering. **Recommendation**: Replace `ui.table()` with manual row rendering for session list to enable per-column colors.

### Lineage
5. **Goal text**: Currently uses default color (no `.fg()` call) -- add `.fg(text)` explicitly
6. **Already well-differentiated** -- lineage view is the best-colored view currently

### Debug
7. **Data preview**: Change from `dim` to `surface_text` to differentiate from labels
8. **Aggregate ID**: Already correctly `dim` (it's supplementary)

### Execution
9. **Event detail text**: Change from `dim` to `surface_text` for same reason as debug

### General
10. **No changes needed for**: header, tab bar, footer, logs filter bar -- these already follow the system correctly
