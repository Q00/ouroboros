# Story 4.2: Lateral Thinking Personas

Status: ready-for-dev

## Story

As a developer,
I want alternative thinking strategies,
so that stagnation is broken through creative approaches.

## Acceptance Criteria

1. Five personas defined: Hacker, Researcher, Simplifier, Architect, Contrarian
2. Each persona has distinct prompt strategy
3. Persona can break stagnation pattern
4. Persona results integrated back to main flow
5. Persona switch logged
6. Persona execution timeout (configurable, default 5 min) triggers next persona in rotation

## Tasks / Subtasks

- [ ] Task 1: Define personas (AC: 1, 2)
  - [ ] Create resilience/personas.py
  - [ ] Define Persona enum
  - [ ] Create prompt templates for each
- [ ] Task 2: Implement lateral engine (AC: 3)
  - [ ] Create resilience/lateral.py
  - [ ] Implement think() method
  - [ ] Apply persona strategy
- [ ] Task 3: Integrate results (AC: 4, 5)
  - [ ] Validate persona output
  - [ ] Merge back to main context
  - [ ] Log persona switch
- [ ] Task 4: Handle persona timeout (AC: 6)
  - [ ] Add persona_timeout_seconds config option (default 300)
  - [ ] Wrap persona execution in timeout context
  - [ ] On timeout, log event and trigger next persona in rotation
  - [ ] Emit PERSONA_TIMEOUT event with persona name and duration
- [ ] Task 5: Write tests
  - [ ] Create unit tests in tests/unit/
  - [ ] Achieve ≥80% coverage
  - [ ] Test error cases

## Dev Notes

- Hacker: unconventional solutions
- Researcher: gather more info
- Simplifier: reduce complexity
- Architect: restructure approach
- Contrarian: challenge assumptions

### Dependencies

**Requires:**
- Story 0-5 (LLM for persona execution)

**Required By:**
- Story 4-3

### References

- [Source: requirement/2_FULL_SPECIFICATION.md#Lateral-Thinking-Personas]

## Dev Agent Record

### Agent Model Used
### Debug Log References
### Completion Notes List
### File List
