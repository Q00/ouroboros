# Antigravity stream contract

A local Intel macOS `agy --help` check on 2026-10-17 advertises
`--output-format text|json|stream-json`. Task-owned headless probes with
`--output-format stream-json --effort low --print-timeout 30s` emitted nested
`init`, `step_update`, and `result` envelopes. This is CLI evidence, separate
from the synthetic regression fixtures. No private session database was read.

The normalizer understands assistant `text_delta` fragments, native
`step_type=tool` events (`tool_info.parameters`), the `tool_call` fixture
spelling (`tool_input`), and terminal `result.response`. It preserves session
identifiers and errors. A live file-read attempt emitted `view_file` with
`AbsolutePath`, followed by a tool error because the test file had not yet been
created. That capture verifies error-event shape, not successful file reading.

Known native file, search, and command tools receive canonical names and
path/command hints. Original arguments remain available; malformed inputs do
not become invented file paths or commands. Unknown tools retain their names.
Tool output is kept separate from assistant answer accumulation. Deltas are
concatenated exactly, and a terminal full response replaces accumulated text.
Plain-text fallback still joins lines for compatibility with wrapped output.

This change supplies parsing and conversion. Enabling structured invocation
and validating workspace/resume behavior are separate runtime changes.
