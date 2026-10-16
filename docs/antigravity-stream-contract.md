# Antigravity stream contract

A local Intel macOS `agy --help` check on 2026-10-17 advertises
`--output-format text|json|stream-json`. Task-owned headless probes with
`--output-format stream-json --effort low --print-timeout 30s` emitted nested
`init`, `step_update`, and `result` envelopes. This is CLI evidence, separate
from the synthetic regression fixtures. No private session database was read.

The normalizer understands assistant `text_delta` fragments, native
`step_type=tool` events (`tool_info.parameters`), the `tool_call` fixture
spelling (`tool_input`), and terminal `result.response`. It preserves session
identifiers and errors. One live file-read probe emitted `view_file` with
`AbsolutePath`, followed by a tool error because the test file had not yet been
created. A separate probe read an existing test file successfully; its DONE
event used `tool_info.output` (for example, `2 lines, 21 bytes`). The successful
capture also ended with a terminal `result.response`. These are observations
from the local CLI probes, distinct from the synthetic fixtures.

Known native file, search, and command tools receive canonical names and
path/command hints. Original arguments remain available; malformed inputs do
not become invented file paths or commands. Unknown tools retain their names.
Tool output is kept separate from assistant answer accumulation. Deltas are
concatenated exactly, and a terminal full response replaces accumulated text.
Plain-text fallback still joins lines for compatibility with wrapped output.

Workspace and resume validation are separate runtime changes.

## Runtime invocation

The runtime now requests `--output-format stream-json` and advertises structured
output. Native tool-use and tool-result messages retain canonical inputs in the
runtime transcript. Plain-text wrapped output remains a compatibility fallback.
The existing runtime-message projection counts observed calls. Canonical Write
and Edit paths reach the existing verification-report renderer and then
ArtifactCollector; a regression disables directory scanning to verify that
native path hints drive collection. Plain-text fallback logs that tool usage is
unknown: zero observed calls does not establish that no tools were used.
The adapter cannot reconstruct events omitted by the CLI itself.


## Targeted resume

The runtime forwards an explicit native conversation ID or a resumable runtime
handle through `--conversation ID`. It does not use `--continue`, which selects
the most recent conversation rather than the requested one. A task-owned live
headless probe continued the same native ID and recalled the prior test marker;
its result reported two turns. Unsupported CLI versions return their resume
error; the adapter never retries that request as a new conversation.

Structured events must keep the native conversation ID of an existing handle.
A mismatched ID, including one in the initial event, produces a terminal
`ConversationMismatch` error before that event's content is delivered. The
underlying invocation is closed and is not retried as a fresh session. Streams
keep their checks in the existing invocation-local handles, so concurrent and
same-task interleaved consumers do not share mutable expected-ID state.


## Workspace binding

The prompt explicitly identifies the resolved requested working directory and
the subprocess starts there. A native init reporting another absolute cwd
fails the task and stops further event processing. A matching init produces a
workspace-binding observation; missing cwd or plain output produces an explicit
unverified-workspace message. These checks observe and reject a mismatch after
startup; they are not a filesystem sandbox and cannot undo work the child has
already performed. `--add-dir` is not used as evidence of replacement or
confinement. A live IDE-daemon workspace check is recorded separately from unit
fixtures and standalone headless probes.


## Native effort and permissions

Supported per-invocation efforts (`low`, `medium`, `high`, `xhigh`, `max`) map to
`--effort VALUE`; other values fail before launch. No effort input leaves the
CLI default unchanged. Capability reporting advertises that exact native set.
A task-owned live `-p --effort low` probe completed successfully.

The existing `acceptEdits`, `bypassPermissions`, and normalized `default`
settings all keep `--dangerously-skip-permissions`. This is full auto-approval,
including shell tools; the setting name does not imply edit-only permission.
A live skip-flag probe reported `always-proceed`; a separate native
`--mode accept-edits` probe reported `request-review`. Those observations do
not establish equivalent headless approval semantics, so this change does not
replace the established bypass mapping with a different mode. Native `auto`
was unavailable for the test account and warned that it fell back to default;
that probe is not evidence of enforced auto mode.
