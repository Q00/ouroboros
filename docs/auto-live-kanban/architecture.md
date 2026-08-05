# Auto Live Kanban Architecture

> Generated: 2026-08-06

## Overview

The existing DB-scoped singleton daemon remains the only HTTP server. The read-only
reader now projects recent execution summaries for the picker, while the existing
`EventTail` and `reduce_board` path remains the source of truth for a selected run.

```text
EventStore SQLite (read-only)
        ├── /api/runs → recent summaries → run list
        └── /events?run=id → EventTail → reduce_board → live detail Kanban
```

## Components

| Component | Responsibility |
|---|---|
| `dashboard_web/reader.py` | Preserve start-event `seed_goal`; project status/progress/provider summaries for concurrent runs |
| `dashboard_web/page.py` | Render list by default, detail when `?run=` is present, and keep goal text untrimmed |
| `dashboard_web/server.py` | Continue serving `/api/runs`, `/events`, and snapshots from one daemon |
| `cli/commands/run.py` | Print a pinned detail URL before execution begins |
| `cli/commands/auto.py` | Print the unpinned list URL while interview/Seed work has no execution ID |

## URL Contract

- `/` — live run picker
- `/?run=<execution_id>` — live detail board
- `/snapshot?run=<execution_id>` — frozen detail board

Execution IDs are URL-encoded at URL construction and again when the browser opens
the SSE stream.
