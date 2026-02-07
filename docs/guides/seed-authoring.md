# Seed Authoring Guide

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
  - "Python 3.14+ with stdlib only"
  - "Must work offline"
  - "Response time under 100ms for all operations"
```

**Tips**:
- Be specific: "No external dependencies" rather than "Keep it simple"
- Constraints are immutable after seed generation
- The evaluation pipeline checks artifacts against constraints

### acceptance_criteria (required)

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
  - "Python 3.14+"
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

Validate a seed without executing:

```bash
uv run ouroboros run seed.yaml --dry-run
```

This checks:
- YAML syntax
- Required fields present
- Field types correct
- Ambiguity score in range
- Ontology schema valid
