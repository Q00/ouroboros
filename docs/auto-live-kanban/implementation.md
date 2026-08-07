# Auto Live Kanban Implementation

> Completed: 2026-08-06

## Summary

Added a multi-run live dashboard picker. The singleton daemon now exposes rich run
summaries, the browser shows the picker at the base URL, and each row links to a
query-parameter-pinned live Kanban. Goal text is preserved without trimming or
client-side truncation.

## Files Modified

- `src/ouroboros/dashboard_web/reader.py` — run summary projection and terminal event coverage.
- `src/ouroboros/dashboard_web/page.py` — list/detail views and concurrent-run polling UI.
- `src/ouroboros/dashboard_web/daemon.py` — safe query encoding for detail URLs.
- `src/ouroboros/dashboard_web/__main__.py` — CLI run list output.
- `src/ouroboros/cli/commands/run.py` — pinned live dashboard guidance.
- `src/ouroboros/cli/commands/auto.py` — list dashboard guidance before execution ID allocation.
- `src/ouroboros/mcp/tools/_dashboard.py` — updated base URL contract.

## Testing

```text
uv run pytest -q tests/unit/dashboard_web/test_page.py tests/unit/dashboard_web/test_daemon.py tests/unit/dashboard_web/test_reader_readonly_uri.py tests/unit/cli/test_auto_command.py tests/unit/mcp/tools/test_start_auto.py
127 passed
```

Static analysis:

```text
uv run ruff check <changed Python files>
All checks passed
```
