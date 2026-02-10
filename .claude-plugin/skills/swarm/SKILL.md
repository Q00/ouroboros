# /ouroboros:swarm

Coordinated multi-agent team working on a shared goal.

## Usage

```
ooo swarm "<your request>"
/ouroboros:swarm "<your request>"
```

**Trigger keywords:** "swarm", "team", "coordinated", "multi-agent"

## How It Works

Swarm mode coordinates specialized agents as a team:

1. **Assemble Team** (planner)
   - Select appropriate specialists
   - Define roles and responsibilities
   - Establish communication protocol

2. **Assign Tasks** (team lead)
   - Distribute work to team members
   - Set dependencies between agents
   - Establish handoff points

3. **Coordinate** (orchestrator)
   - Agents work in parallel where possible
   - Handle inter-agent communication
   - Aggregate results

4. **Integrate** (integrator)
   - Combine outputs from all agents
   - Resolve conflicts
   - Verify final result

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what needs to be done

2. **Initialize state**: Create `.omc/state/swarm-state.json`:
   ```json
   {
     "mode": "swarm",
     "session_id": "<uuid>",
     "request": "<user request>",
     "status": "assembling",
     "team": [],
     "tasks": {},
     "messages": []
   }
   ```

3. **Delegate to planner** to assemble team:
   ```
   Assemble a team for: "<user request>"

   Identify:
   1. What specialist roles are needed
   2. What each agent will be responsible for
   3. How agents should coordinate

   Output:
   - Team composition (list of agent roles)
   - Task assignments per agent
   - Coordination protocol
   ```

4. **Create team using TeamCreate**:
   ```
   TeamCreate(
     team_name="swarm-{session_id}",
     description="Swarm execution for: {request}",
     agent_type="planner"  # Team lead
   )
   ```

5. **Create tasks using TaskCreate** for each team member:
   ```
   TaskCreate(
     subject="<agent's task>",
     description="<detailed task>",
     activeForm="<doing task>",
     metadata={"agent_role": "<role>"}
   )
   ```

6. **Spawn team members** using Task:
   ```
   for role in team_roles:
       Task(
         subagent_type=f"oh-my-claudecode:{role}",
         team_name=team_name,
         name=role
       )
   ```

7. **Coordinate execution**:
   - Monitor task completion via TaskList
   - Handle inter-agent messages via SendMessage
   - Update state with progress

8. **Aggregate results**:
   - After all tasks complete, combine outputs
   - Resolve any conflicts
   - Run final verification

## Team Composition

Common swarm patterns:

| Request Type | Team Roles |
|--------------|------------|
| New feature | analyst, planner, executor, test-engineer, verifier |
| Bug fix | debugger, executor, test-engineer |
| Architecture | architect, analyst, planner |
| Full product | product-manager, ux-researcher, designer, executor |

## Agent Communication

Use SendMessage for coordination:
- `type="message"` - Direct message to specific agent
- `type="broadcast"` - All agents (use sparingly)
- `type="shutdown_request"` - Gracefully terminate agent

## Example

```
User: ooo swarm build a REST API with tests

[Swarm Assembling]
Planning team composition...

Team:
- planner (team lead)
- executor (implementation)
- test-engineer (test coverage)
- verifier (quality assurance)

[Swarm Task Assignment]
planner: Create execution plan
executor: Implement API endpoints
test-engineer: Write comprehensive tests
verifier: Validate implementation

[Swarm Execution]
planner: Plan created
executor: Implementing endpoints...
test-engineer: Writing tests...
verifier: Waiting for implementation...

executor: Implementation complete
test-engineer: Tests written
verifier: Verifying...

[Swarm Integration]
Aggregating results...
All tests passing...

Swarm COMPLETE
===============
Request: REST API with tests
Duration: 5m 30s
Team Size: 4

Team Contributions:
- planner: Execution plan
- executor: 3 API endpoints implemented
- test-engineer: 12 tests written
- verifier: All checks passed

Result: PASSED
```

## Message Example

```python
# Delegate work to a teammate
SendMessage(
    type="message",
    recipient="executor",
    content="Please implement the DELETE endpoint now",
    summary="Assigning DELETE endpoint"
)

# Broadcast to all (emergency only)
SendMessage(
    type="broadcast",
    content="Critical bug found! Stop all work.",
    summary="URGENT: Stop work"
)
```

## State Persistence

Track:
- Team composition and member IDs
- Task assignments and status
- Inter-agent messages
- Aggregated results

## Team Lifecycle

1. `TeamCreate` - Create team and task list
2. `TaskCreate` x N - Create tasks
3. `Task(team_name, name)` x N - Spawn teammates
4. Agents claim tasks via `TaskUpdate(owner="name")`
5. Agents complete tasks and mark via `TaskUpdate(status="completed")`
6. `SendMessage(shutdown_request)` - Request shutdown
7. `TeamDelete` - Clean up team resources

## Cancellation

Cancel with `/ouroboros:cancel`:

1. Send `shutdown_request` to all teammates
2. Wait for graceful shutdown
3. Call `TeamDelete` to clean up
4. State is saved for potential resume
