# #978 TraceGuard observation protocol

This document defines the reproducible observation loop for #978 / #961 after the
fat-harness default flip and before any #978 P5 legacy self-report fallback
removal.

## Scope

This protocol is observation-only. It does not authorize:

- removing the legacy self-report fallback;
- changing default `ooo run` behavior;
- adding a new AgentOS substrate surface;
- treating one controlled run as P5 readiness.

The goal is to prove, with EventStore evidence, that fat-harness atomic AC
acceptance can complete through:

```text
typed evidence present -> schema-valid typed evidence -> verifier ran -> verifier PASS
```

## Required commit preflight

Run observations from a clean clone, not from a dirty development checkout.

```bash
rm -rf /tmp/ouroboros-observation-main
git clone https://github.com/Q00/ouroboros.git /tmp/ouroboros-observation-main
cd /tmp/ouroboros-observation-main
git checkout main
git pull --ff-only
git log --oneline -8
```

For post-#1026 observation, `main` must include the merge commit for PR #1026.
If #1026 is not merged yet, the run is diagnostic only and must not be recorded
as clean-main P5-readiness evidence.

## Controlled seed: non-overlapping positive path

Use one atomic AC that creates both the implementation and its test. This avoids
the known overlap in the earlier two-AC seed where AC1 created `test_hello.py`,
then AC2 could not produce fresh `files_touched` evidence for the same file.

Create `/tmp/character-chat-978-observation/controlled-hello-seed-978.yaml`:

```yaml
goal: "Create a minimal Python hello function with a pytest verification."

constraints:
  - "Use Python only."
  - "Do not add external dependencies beyond pytest."
  - "Keep the implementation minimal."

acceptance_criteria:
  - |
    Create hello.py with a hello() function that returns exactly 'hello'.
    Create test_hello.py with a pytest test proving hello() returns exactly
    'hello'. Run python -m pytest test_hello.py successfully.

ontology_schema:
  name: "ControlledHello"
  description: "Minimal controlled seed for #978 typed evidence observation."
  fields:
    - name: "hello_function"
      field_type: "function"
      description: "hello() returns exactly hello."

metadata:
  seed_id: "controlled_hello_978_single_ac"
  ambiguity_score: 0.0
```

## Run command

```bash
rm -rf /tmp/character-chat-978-observation
mkdir -p /tmp/character-chat-978-observation
cd /tmp/character-chat-978-observation
git init
cat > controlled-hello-seed-978.yaml <<'YAML'
name: "Controlled hello #978 observation"
description: "Single-AC observation seed for post-#1026 TraceGuard evidence gate."

constraints:
  - "Use Python only."
  - "Do not add external dependencies beyond pytest."
  - "Keep the implementation minimal."

acceptance_criteria:
  - |
    Create hello.py with a hello() function that returns exactly 'hello'.
    Create test_hello.py with a pytest test proving hello() returns exactly
    'hello'. Run python -m pytest test_hello.py successfully.

ontology_schema:
  name: "ControlledHello"
  description: "Minimal controlled seed for #978 typed evidence observation."
  fields:
    - name: "hello_function"
      field_type: "function"
      description: "hello() returns exactly hello."

metadata:
  seed_id: "controlled_hello_978_single_ac"
  ambiguity_score: 0.0
YAML

PYTHONPATH=/tmp/ouroboros-observation-main/src \
uv --project /tmp/ouroboros-observation-main run ouroboros run controlled-hello-seed-978.yaml \
  2>&1 | tee observation-controlled-run-post-1026.log
```

## EventStore summary query

```bash
python3 - <<'PY'
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

db = Path.home() / ".ouroboros" / "ouroboros.db"
con = sqlite3.connect(db)

rows = con.execute(
    """
    select timestamp, payload
    from events
    where event_type = 'execution.ac.typed_evidence.observed'
    order by timestamp desc
    """
).fetchall()

by_exec = defaultdict(list)
for ts, payload in rows:
    data = json.loads(payload) if isinstance(payload, str) else payload
    execution_id = data.get("execution_id") or "unknown"
    by_exec[execution_id].append((ts, data))

latest_exec = next(iter(by_exec), None)
print("latest_execution_id:", latest_exec)

events = by_exec[latest_exec]
print("typed_evidence_event_count:", len(events))

for key in [
    "enforced",
    "fat_harness_mode",
    "typed_evidence_present",
    "typed_evidence_valid",
    "typed_evidence_error",
    "verifier_ran",
    "verifier_passed",
    "verifier_failure_class",
    "enforcement_error",
]:
    counts = Counter(str(data.get(key)) for _, data in events)
    print(key, dict(counts))
PY
```

## Legacy fallback log check

```bash
grep -iE "legacy|self.report|self-report|self_report|fallback" \
  /tmp/character-chat-978-observation/observation-controlled-run-post-1026.log || true
```

Treat this as a log-signal check only. Absence of a grep hit does not by itself
prove every internal fallback branch is unreachable.

## Positive controlled signal criteria

A controlled run is a positive signal only when at least one observed AC has:

- `typed_evidence_present=true`
- `typed_evidence_valid=true`
- `verifier_ran=true`
- `verifier_passed=true`
- no visible legacy self-report fallback acceptance signal

A full controlled pass is stronger and should also show the CLI run succeeds.

## Negative / blocked outcomes

- `typed_evidence_present=false` or `typed_evidence_valid=false`: prompt/extractor/schema seam remains blocked.
- `verifier_ran=false`: verifier invocation is still gated before it can judge evidence.
- `verifier_passed=false`: inspect `verifier_reasons` / `enforcement_error`; this is usually an evidence-matching or real-failure blocker.
- Manual pytest passing while verifier fails: implementation may be correct, but P5 remains blocked because acceptance did not pass through the evidence gate.

## Reporting template

Post the result on #978, and summarize the state on #961 if it changes the SSOT.

```md
## #978 Observation batch N — post-#1026 clean-main controlled run

Date: YYYY-MM-DD TZ
Ouroboros commit: <sha>
Target repo: /tmp/character-chat-978-observation
Seed: controlled-hello-seed-978.yaml

Run command:
```bash
PYTHONPATH=/tmp/ouroboros-observation-main/src uv --project /tmp/ouroboros-observation-main run ouroboros run controlled-hello-seed-978.yaml
```

CLI result:
- Total ACs:
- Succeeded:
- Failed:
- Skipped:
- Exit status:

Typed evidence:
- execution id:
- event count:
- typed_evidence_present=true:
- typed_evidence_valid=true:
- verifier_ran=true:
- verifier_passed=true:
- typed_evidence_error values:
- enforcement_error values:

Legacy fallback:
- Used / not observed / unknown:
- Evidence:

Manual sanity check:
- Files created:
- `python -m pytest test_hello.py` result:

Conclusion:
- Positive controlled signal: yes/no
- #978 P5 remains blocked: yes/no
- Next observation/fix needed:
```

## P5 readiness boundary

A positive controlled run is not enough to open #978 P5. Per #961, fallback
removal still requires a later release/usage observation cycle that shows no
regressive fabrication or semantic-miss signal under normal usage.
