<!--
doc_metadata:
  runtime_scope: [all]
-->

# Seed Authoring Guide (Advanced)

> **Prerequisites:** This is an advanced guide for manually authoring or customizing seeds. If you're new to Ouroboros, start with the [Getting Started guide](../getting-started.md) -- the recommended flow auto-generates a seed from the interview step (`ooo interview` / `ouroboros interview`), and most users never need to write one by hand.

The Seed is Ouroboros's immutable specification -- a "constitution" that drives execution, evaluation, and drift control. This guide covers the YAML structure, field semantics, and best practices for writing effective seeds.

## Seed YAML Schema

```yaml
# Required fields
goal: "<primary objective>"
acceptance_criteria:
  - "<criterion 1>"
  - "<criterion 2>"
ontology_schema:
  name: "<schema name>"
  description: "<schema purpose>"
  fields:
    - name: "<field>"
      field_type: "<type>"
      description: "<purpose>"
metadata:
  ambiguity_score: <0.0-1.0>

# Optional fields
task_type: "code"           # "code" (default), "research", or "analysis"
constraints:
  - "<constraint>"
evaluation_principles:
  - name: "<principle>"
    description: "<what it evaluates>"
    weight: <0.0-1.0>
exit_conditions:
  - name: "<condition>"
    description: "<when to terminate>"
    evaluation_criteria: "<how to check>"
```

## Field Reference

### goal (required)

The primary objective. Must be a non-empty string. This is the single most important field -- everything else serves this goal.

```yaml
# Good: specific, measurable, bounded
goal: "Build a Python CLI tool that converts CSV files to JSON with column type inference"

# Bad: vague, unbounded
goal: "Make something that handles data"
```

### task_type (optional, default: "code")

Controls execution strategy (tools, prompts) and evaluation behavior.

| Value | Execution Tools | Evaluation Focus | Output Format |
|-------|----------------|-----------------|---------------|
| `code` | Read, Write, Edit, Bash, Glob, Grep | Lint, build, test, semantic | Source code files |
| `research` | Read, Write, Bash, Glob, Grep | Structure, references, completeness | Markdown documents |
| `analysis` | Read, Write, Bash, Glob, Grep | Structure, reasoning quality | Markdown documents |

```yaml
task_type: research
```

### constraints (optional)

Hard requirements that must always be satisfied. These are non-negotiable.

```yaml
constraints:
  - "Python >= 3.12 with stdlib only"
  - "Must work offline"
  - "Response time under 100ms for all operations"
```

**Tips**:
- Be specific: "No external dependencies" rather than "Keep it simple"
- Constraints are immutable after seed generation
- The evaluation pipeline checks artifacts against constraints

### acceptance_criteria (recommended — strongly advised but not schema-enforced)

Specific, testable criteria for success. Each AC becomes a node in the execution tree and is evaluated independently.

```yaml
acceptance_criteria:
  # AC1: Foundation
  - "Create a User model with id, name, email fields and SQLite persistence"
  # AC2: Depends on AC1
  - "Implement CRUD operations for User: create, read, update, delete"
  # AC3: Independent
  - "Add input validation with clear error messages for all fields"
```

**Writing effective ACs**:

1. **One concern per AC**: Each AC should address one feature or capability
2. **Testable**: Should be verifiable by mechanical checks or semantic evaluation
3. **Ordered by dependency**: If AC2 needs AC1's output, list AC1 first
4. **Specific deliverables**: Name files, functions, or document sections explicitly

```yaml
# Good: specific, testable, has clear deliverables
acceptance_criteria:
  - |
    Create utils/string_helpers.py with:
    - slugify(text: str) -> str: Convert text to URL-friendly slug
    - truncate(text: str, max_len: int) -> str: Truncate with "..." suffix
    Include tests/test_string_helpers.py with at least 3 tests per function

# Bad: vague, not testable
acceptance_criteria:
  - "Handle string processing"
```

**Multi-line ACs**: Use YAML block scalars for complex criteria:

```yaml
acceptance_criteria:
  - |
    Create the authentication module:
    1. src/auth.py with generate_token() and validate_token()
    2. tests/test_auth.py with at least 3 tests
    3. Token expiry must be configurable via Config
  - >
    Add structured logging that integrates
    with the existing config module and
    outputs JSON-formatted log entries.
```

### ontology_schema (required)

Defines the conceptual structure of what the workflow produces.

```yaml
ontology_schema:
  name: "TaskManager"
  description: "Domain model for task management"
  fields:
    - name: "task"
      field_type: "entity"
      description: "A unit of work with status tracking"
      required: true
    - name: "priority"
      field_type: "enum"
      description: "Task priority level: low, medium, high"
    - name: "status_transition"
      field_type: "action"
      description: "State change from one status to another"
```

**Field types**: Use descriptive strings -- `entity`, `action`, `string`, `number`, `boolean`, `array`, `object`, `enum`. These guide the LLM's understanding of domain structure.

### evaluation_principles (optional)

Principles for evaluating output quality, with relative weights.

```yaml
evaluation_principles:
  - name: "correctness"
    description: "Implementation matches specification exactly"
    weight: 1.0
  - name: "testability"
    description: "All public functions have corresponding tests"
    weight: 0.9
  - name: "simplicity"
    description: "No unnecessary abstraction or over-engineering"
    weight: 0.7
```

### exit_conditions (optional)

Conditions for terminating the workflow.

```yaml
exit_conditions:
  - name: "all_ac_met"
    description: "All acceptance criteria pass evaluation"
    evaluation_criteria: "Stage 2 score >= 0.8 for all ACs"
  - name: "max_iterations"
    description: "Safety limit on iteration count"
    evaluation_criteria: "Stop after 5 full cycles"
```

### metadata (required)

Generation metadata. When writing seeds manually, provide at minimum:

```yaml
metadata:
  seed_id: "my_project_001"    # Unique identifier
  ambiguity_score: 0.1         # 0.0-1.0, lower is clearer (required)
```

Optional metadata fields:
- `version`: Schema version (default: "1.0.0")
- `created_at`: ISO timestamp (auto-generated)
- `interview_id`: Reference to source interview

## Complete Examples

### Code Task: REST API

```yaml
goal: "Build a REST API for a todo application using Python and FastAPI"
task_type: code

constraints:
  - "Python >= 3.12"
  - "FastAPI framework"
  - "SQLite database via SQLAlchemy"
  - "Must include OpenAPI documentation"

acceptance_criteria:
  - "Create database models for Todo items (id, title, description, completed, created_at)"
  - "Implement CRUD endpoints: POST /todos, GET /todos, GET /todos/{id}, PUT /todos/{id}, DELETE /todos/{id}"
  - "Add input validation with Pydantic models and proper HTTP error responses"
  - "Write integration tests covering all endpoints with at least 90% coverage"

ontology_schema:
  name: "TodoAPI"
  description: "REST API domain for todo management"
  fields:
    - name: "todo"
      field_type: "entity"
      description: "A todo item"
    - name: "endpoint"
      field_type: "action"
      description: "An API endpoint"

evaluation_principles:
  - name: "api_correctness"
    description: "All endpoints return correct status codes and response bodies"
    weight: 1.0
  - name: "test_coverage"
    description: "Integration tests cover happy and error paths"
    weight: 0.9

metadata:
  seed_id: "todo_api_001"
  ambiguity_score: 0.12
```

### Research Task: Technology Comparison

```yaml
goal: "Research and compare message queue technologies for a high-throughput event processing system"
task_type: research

constraints:
  - "Focus on RabbitMQ, Apache Kafka, and Redis Streams"
  - "Consider throughput > 100k events/sec requirement"
  - "Cloud-native deployment (Kubernetes)"

acceptance_criteria:
  - "Produce a feature comparison matrix covering throughput, latency, durability, and ordering guarantees"
  - "Analyze operational complexity for each option (deployment, monitoring, scaling)"
  - "Provide a cost analysis for 1M events/day on AWS"
  - "Write a recommendation with clear rationale"

ontology_schema:
  name: "MessageQueueComparison"
  description: "Comparative analysis of message queue systems"
  fields:
    - name: "technology"
      field_type: "entity"
      description: "A message queue technology"
    - name: "criterion"
      field_type: "entity"
      description: "An evaluation criterion"

metadata:
  seed_id: "mq_comparison_001"
  ambiguity_score: 0.15
```

### Analysis Task: Architecture Decision

```yaml
goal: "Analyze the trade-offs between microservices and monolith architecture for our e-commerce platform"
task_type: analysis

constraints:
  - "Team size: 8 developers"
  - "Current monolith: 50k LOC Python Django"
  - "Must consider migration cost"

acceptance_criteria:
  - "Map current system components and their coupling"
  - "Identify 3-5 service boundary candidates with dependency analysis"
  - "Calculate migration effort estimate for each candidate"
  - "Produce a phased migration roadmap with risk assessment"

ontology_schema:
  name: "ArchitectureDecision"
  description: "Architecture trade-off analysis"
  fields:
    - name: "component"
      field_type: "entity"
      description: "A system component or service"
    - name: "dependency"
      field_type: "relation"
      description: "Coupling between components"

metadata:
  seed_id: "arch_decision_001"
  ambiguity_score: 0.18
```

## Parallel Execution Tips

When ACs have dependencies, Ouroboros automatically detects them and schedules independent ACs in parallel. To help the dependency analyzer:

1. **Mention dependencies explicitly**: "depends on AC1's config module" helps the analyzer
2. **Name shared files**: "Both AC2 and AC3 modify `src/config.py`" signals potential conflicts
3. **Order by dependency**: List foundation ACs first, integration ACs last

```yaml
acceptance_criteria:
  # Level 1: Foundation (runs first)
  - "Create shared config module and base models"
  # Level 2: Parallel (both depend on Level 1, independent of each other)
  - "Add authentication module (depends on config and models)"
  - "Add logging module (depends on config)"
  # Level 3: Integration (depends on Level 2)
  - "Create app entry point that integrates auth and logging"
```

## Validation

> **Note — `--dry-run` is not functional in the current implementation.** In the default orchestrator mode (`--orchestrator` is `True` by default), the `--dry-run` flag is silently ignored and execution proceeds normally. In non-orchestrator mode (`--no-orchestrator`), `--dry-run` prints a placeholder message without performing any YAML or schema checks. This limitation is tracked for a future release.

**Current approach to pre-run validation:** Run the workflow normally. Schema validation errors surface *before* any agent sessions start, so an invalid seed will print an error and exit without executing:

```bash
# Claude Code path
ooo run seed.yaml

# Standalone CLI path
ouroboros run seed.yaml
```

If the seed is malformed, you will see errors like:

```
Error: Invalid seed format: 1 validation error for Seed
  goal
    Field required [type=missing, ...]
```

The following checks are enforced by Pydantic schema validation when the seed is loaded:
- YAML syntax (file must be valid YAML)
- `goal` present and non-empty
- `ontology_schema` present with `name` and `description`
- `metadata` present
- `ambiguity_score` in range (0.0–1.0)
- `weight` on each evaluation principle in range (0.0–1.0)
- Seed YAML file size under 1 MB

**Note:** `acceptance_criteria` is optional in the schema — an empty list is accepted and will not raise a validation error. If you omit acceptance criteria, the orchestrator will execute with no criteria to evaluate, which is rarely intentional.

---

## Failure Modes & Troubleshooting

The seed creation workflow has three phases where failures can occur:

1. **Interview phase** (`ooo interview` / `ouroboros interview`) — LLM generates clarifying questions
2. **Ambiguity scoring phase** — LLM scores the collected answers
3. **Seed generation & save phase** — LLM extracts requirements and writes the YAML file

### Phase 1: Interview Failures

#### Missing or invalid API key

**Symptom:**
```
Error: Failed to start interview: Authentication error: invalid API key
```

**Cause:** `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is not set, expired, or incorrect.

**Fix:**
```bash
# For LiteLLM (default) mode
export ANTHROPIC_API_KEY="sk-ant-..."
# or
export OPENAI_API_KEY="sk-..."

# To avoid needing an API key, use Claude Code (Max Plan):
ooo interview "Build a REST API"
# or standalone:
ouroboros interview start --orchestrator "Build a REST API"
```

#### LLM rate-limit or transient API error during questioning

**Symptom:**
```
Error: Failed to generate question: Rate limit exceeded
Retry? [Y/n]:
```

**Behavior:** The interview engine shows the error and prompts whether to retry. The interview session state is preserved. Answering `Y` (default) retries the question generation. Answering `N` exits the round loop and moves you to seed generation with the rounds collected so far.

#### State save warning (non-fatal)

**Symptom:**
```
Error: Warning: Failed to save state: [Errno 13] Permission denied: '...'
```

**Behavior:** This is a **warning only** — the interview continues. However, your progress will not be saved for resumption if the session ends. Fix the directory permissions (see [File System Errors](#file-system-errors)) and restart if needed.

#### Empty response rejected

**Symptom:**
```
Error: Response cannot be empty. Please try again.
```

**Behavior:** Empty answers are never accepted. The current question is re-displayed. Provide a non-empty answer to continue.

#### Interrupted with Ctrl+C

**Behavior:** The interview is interrupted cleanly and all completed rounds are saved:
```
Interview interrupted. Progress has been saved.
```

The session can be resumed:
```bash
ouroboros interview list                                    # find the session ID
ouroboros interview start --resume interview_20260125_120000      # resume it
```

Exit code is `0` (not an error).

#### EOF / stdin closed mid-interview

**Symptom:**
```
Interview failed: EOF when reading a line
```

**Cause:** Standard input was closed while the interview prompt was waiting for input. This happens when running non-interactively (e.g., piped input that ends before the interview finishes) or when the terminal is closed.

**Behavior:** The outer error handler catches `EOFError` as a generic exception, prints the error, and exits with code `1`. Progress up to the last completed round is saved (state is persisted after each recorded response).

**Fix:** Run `ouroboros interview` in an interactive terminal. If you must automate input, pipe the full conversation and ensure the stream stays open until the interview completes.

#### Input context too long

**Symptom:**
```
Error: Failed to start interview: Initial context exceeds maximum length (50000 chars)
```

**Cause:** The initial context or idea passed to `ouroboros init` exceeds 50,000 characters.

**Behavior:** The interview is never started; the command exits immediately with code `1`.

**Fix:** Shorten your initial context. If the idea is inherently large (e.g., pasting a full specification), summarize it into a concise goal statement and let the interview draw out the details.

#### Response too long

**Symptom:**
```
Error: Failed to record response: Response exceeds maximum length (10000 chars)
```

**Cause:** A single interview answer exceeds 10,000 characters.

**Behavior:** The answer is **not recorded** and the current question is displayed again. The interview continues normally.

**Fix:** Break large pasted content into shorter answers across multiple rounds, or summarize.

#### Whitespace-only input

**Symptom (initial context):**
```
Error: Failed to start interview: Initial context cannot be only whitespace
```

**Symptom (response):**
```
Error: Response cannot be empty. Please try again.
```

**Behavior:** Both initial context and per-round responses are validated for non-empty, non-whitespace content. Whitespace-only strings are rejected immediately. The interview continues from the current question.

#### Resume with invalid interview ID

**Symptom:**
```
Error: Failed to load interview: Interview not found: interview_bad_id
```

**Fix:** Run `ouroboros interview list` to see valid session IDs.

#### Resume with corrupt or unreadable state file

**Symptom:**
```
Error: Failed to load interview: Failed to load interview state: <parse or I/O error>
```

**Cause:** The state file at `~/.ouroboros/data/interview_<id>.json` exists but cannot be read — either due to permission issues, disk errors, or partial writes that left the JSON malformed.

**Fix:**
1. Check read permissions: `ls -la ~/.ouroboros/data/interview_<id>.json`
2. Inspect the file manually for truncation or obvious corruption.
3. If the file is unrecoverable, start a new interview session. Completed rounds from the old session are not automatically migrated, but you can reference the partial answers to quickly recreate the session.

---

### Phase 2: Ambiguity Scoring Failures

#### LLM API failure during scoring

**Symptom:**
```
Error: Failed to calculate ambiguity: Failed to parse scoring response after 10 attempts: ...
```

**Behavior:** The ambiguity scorer retries automatically up to 10 times total. Token budget doubling only occurs when the LLM response is truncated (`finish_reason == "length"`); provider errors (rate limits, transient failures) and format errors retry with the same token budget. If all 10 attempts are exhausted, the error above is shown and seed generation is **cancelled** (the interview state is preserved).

**Fix:** Check API key validity and quota, then re-run the seed generation by selecting "Proceed to generate Seed specification?" at the post-interview prompt:
```bash
ouroboros interview start --resume interview_20260125_120000
```
The interview session is already complete; you can proceed directly to seed generation.

#### Ambiguity score too high (> 0.20)

**Symptom:**
```
Warning: Ambiguity score (0.45) is too high. Consider more interview rounds to clarify requirements.

What would you like to do?
  1 - Continue interview with more questions
  2 - Generate Seed anyway (force)
  3 - Cancel
```

**Options:**

| Choice | Effect |
|--------|--------|
| `1` (default) | Re-opens the interview for additional questions. The score threshold is re-evaluated after the new round. |
| `2` | Forces seed generation with the current (high-ambiguity) context. The resulting seed may have vague or incomplete acceptance criteria — review it carefully before executing. |
| `3` | Cancels. The interview state is saved; resume with `--resume`. |

**Tips for reducing ambiguity:**
- Provide specific deliverable names (files, functions, endpoints)
- State explicit constraints (language versions, libraries, limits)
- Give measurable success criteria ("at least 90% test coverage")

---

### Phase 3: Seed Generation & Save Failures

#### LLM provider error during requirement extraction

**Symptom:**
```
Error: Failed to generate Seed: <provider error message, e.g. "Rate limit exceeded" or "Connection error">
```

**Cause:** The LLM API call itself failed (network error, rate limit, authentication error) rather than returning a parseable but malformed response.

**Behavior:** Unlike the ambiguity scorer, the seed extractor does **not** retry on provider errors — the error is returned immediately and seed generation is **cancelled** (the interview state is preserved). This is intentional: provider errors in extraction usually indicate a systemic problem (wrong key, quota exhausted) that won't resolve by retrying.

**Fix:** Check your API key and quota, then resume:
```bash
ouroboros interview start --resume interview_20260125_120000
```

#### LLM API response parse failure during requirement extraction

**Symptom:**
```
Error: Failed to generate Seed: Failed to parse extraction response after 2 attempts: Missing required field: goal
```

**Behavior:** The seed generator calls the LLM to extract structured requirements from the interview transcript. It retries once with a simplified prompt if the first response cannot be parsed. If both attempts fail, seed generation is **cancelled** (the interview state is preserved).

**Fix:** Resume the session and try again:
```bash
ouroboros interview start --resume interview_20260125_120000
```
If the model consistently fails to extract a `goal` or `ontology_name`, add more specific answers in additional interview rounds before attempting generation.

#### LLM response parse failure (bad format)

**Symptom (internal log, visible with `--debug`):**
```
seed.extraction.parse_failed  error="Missing required field: ontology_name"  attempt=1
seed.extraction.retry_succeeded  attempt=2
```

**Behavior:** This is handled automatically. The generator retries once with a clarified prompt. No user action needed unless both attempts fail (see above).

#### Seed save failure — permission denied

**Symptom:**
```
Error: Failed to save Seed: [Errno 13] Permission denied: '/root/.ouroboros/seeds/seed_abc123.yaml'
```

**Fix:** Ensure the seeds directory is writable:
```bash
mkdir -p ~/.ouroboros/seeds
chmod 755 ~/.ouroboros/seeds
```

Then resume and re-trigger seed generation:
```bash
ouroboros interview start --resume interview_20260125_120000
```

#### Seed save failure — disk full

**Symptom:**
```
Error: Failed to save Seed: [Errno 28] No space left on device
```

**Fix:** Free disk space, then retry as above.

#### Custom `--state-dir` path does not exist

**Symptom:**
```
Error: Invalid value for '--state-dir': Path '...' does not exist.
```

**Behavior:** Typer validates the path before the interview starts. The command exits immediately.

**Fix:** Create the directory first:
```bash
mkdir -p /path/to/custom/states
ouroboros interview start --state-dir /path/to/custom/states "Build a REST API"
```

---

### File System Errors

The following directories are created automatically if they do not exist:

| Path | Purpose |
|------|---------|
| `~/.ouroboros/data/` | Interview state files (JSON) |
| `~/.ouroboros/seeds/` | Generated seed YAML files |

If automatic creation fails (e.g., due to permissions on `~/.ouroboros/`):

```bash
mkdir -p ~/.ouroboros/data ~/.ouroboros/seeds
chmod 700 ~/.ouroboros
```

---

### Manually Written Seeds

When writing seeds by hand (rather than through the interview), the following schema errors will be caught when you run `ooo run seed.yaml` or `ouroboros run seed.yaml`:

| Error | Cause | Fix |
|-------|-------|-----|
| `yaml.scanner.ScannerError` (or similar) | Invalid YAML indentation or characters | Use a YAML linter; check for tab characters (use spaces only) |
| `1 validation error for Seed\n  goal\n    Field required` | `goal:` key absent | Add a non-empty `goal:` string |
| `1 validation error for Seed\n  ontology_schema\n    Field required` | `ontology_schema:` block absent | Add `ontology_schema:` with `name` and `description` |
| `1 validation error for Seed\n  metadata\n    Field required` | `metadata:` block absent | Add `metadata:` with at least `ambiguity_score: 0.1` |
| `ambiguity_score\n    Input should be less than or equal to 1` | `ambiguity_score` > 1.0 | Use a float between 0.0 and 1.0 |
| `Seed file validation failed: Seed file exceeds maximum size` | Seed YAML > 1 MB | Split into smaller seeds or reduce embedded content |

> **Note:** A missing or empty `acceptance_criteria:` section is **not** a schema validation error — the field is optional and defaults to an empty list. If you omit it, the orchestrator will run without any success criteria to evaluate. Add at least one criterion to get useful execution behavior.

Example minimal valid seed (for testing):

```yaml
goal: "Build a hello-world HTTP server in Python"
acceptance_criteria:
  - "Create server.py that responds with 'Hello, World!' on GET /"
ontology_schema:
  name: "HelloServer"
  description: "Minimal HTTP server"
  fields:
    - name: "endpoint"
      field_type: "action"
      description: "An HTTP route handler"
metadata:
  ambiguity_score: 0.05
```

Check it loads cleanly by running it — any schema or YAML errors will be printed before execution begins:
```bash
ouroboros run minimal_seed.yaml
```

---

### CLI Flag Warnings

#### `--runtime` without `--orchestrator` (init command)

**Symptom:**
```
Warning: --runtime only affects the workflow execution step when --orchestrator is enabled.
```

**Cause:** `--runtime` (e.g., `--runtime codex`) was passed to `ouroboros init` without `--orchestrator`. The `--runtime` flag only controls which agent runtime backend is used when the generated seed is immediately handed off to workflow execution. Without `--orchestrator`, the workflow handoff step uses a placeholder.

**Behavior:** This is a **warning only** — the interview and seed generation proceed normally. The runtime flag has no effect.

**Fix:** Add `--orchestrator` if you want to use the specified runtime backend for the post-generation workflow step:
```bash
ouroboros interview start --orchestrator --runtime codex "Build a REST API"
```

---

### Debugging Tips

Enable verbose output during the interview and seed generation phases with `--debug`:

```bash
ouroboros interview start --debug "Build a REST API"
```

With `--debug` active, the console shows:
- LLM thinking steps (truncated to first 100 characters)
- Tool calls made during brownfield codebase exploration
- Ambiguity scoring component breakdown
- Seed extraction parse attempts and retries

For persistent verbose logging, set `logging.level: debug` in `~/.ouroboros/config.yaml`.
