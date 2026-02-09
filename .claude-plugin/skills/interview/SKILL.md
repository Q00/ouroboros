# /ouroboros:interview

Socratic interview to crystallize vague requirements into clear specifications.

## Usage

```
/ouroboros:interview [topic]
```

**Trigger keywords:** "interview me", "clarify requirements"

## How It Works

1. **Start**: Provide your initial topic or idea
2. **Questioning**: The socratic-interviewer agent asks clarifying questions
3. **Exploration**: Uses Read/Grep/WebFetch to explore context if needed
4. **Iteration**: Continue until you say "done" or requirements are clear
5. **Output**: Interview results ready for seed generation

## Interviewer Behavior

The interviewer is **ONLY a questioner**:
- ✅ Uses Read, Glob, Grep, WebFetch to explore
- ❌ NEVER writes code, edits files, or runs commands
- ✅ Always ends responses with a question
- ✅ Targets the biggest source of ambiguity

## Example Session

```
User: /ouroboros:interview Build a REST API

Interviewer: What domain will this REST API serve?
User: It's for task management

Interviewer: What operations should tasks support?
User: Create, read, update, delete

Interviewer: Will tasks have relationships (e.g., subtasks, tags)?
User: Yes, tags for organizing

User: /ouroboros:seed  [Generate seed from interview]
```

## Next Steps

After interview completion, use `/ouroboros:seed` to generate the Seed specification.
