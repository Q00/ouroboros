# /ouroboros:unstuck

Break through stagnation with lateral thinking personas.

## Usage

```
/ouroboros:unstuck [persona]
```

**Trigger keywords:** "I'm stuck", "think sideways"

## Available Personas

| Persona | Style | When to Use |
|---------|-------|-------------|
| **hacker** | "Make it work first, elegance later" | When overthinking blocks progress |
| **researcher** | "What information are we missing?" | When the problem is unclear |
| **simplifier** | "Cut scope, return to MVP" | When complexity is overwhelming |
| **architect** | "Restructure the approach entirely" | When the current design is wrong |
| **contrarian** | "What if we're solving the wrong problem?" | When assumptions need challenging |

## Instructions

When the user invokes this skill:

1. Determine the context:
   - What is the user stuck on? (Check recent conversation)
   - What approaches have been tried?
   - Which persona would help most?

2. If a specific persona is requested, use it. Otherwise, choose based on context:
   - Repeated similar failures → **contrarian** (challenge assumptions)
   - Too many options → **simplifier** (reduce scope)
   - Missing information → **researcher** (seek data)
   - Analysis paralysis → **hacker** (just make it work)
   - Structural issues → **architect** (redesign)

3. Call the `ouroboros_lateral_think` MCP tool:
   ```
   Tool: ouroboros_lateral_think
   Arguments:
     problem_context: <description of the stuck situation>
     current_approach: <what has been tried>
     persona: "contrarian"  (or chosen persona)
     failed_attempts: ["attempt1", "attempt2"]  (previous failures)
   ```

4. Present the lateral thinking result:
   - Show the persona's approach summary
   - Present the reframing prompt
   - List the questions to consider
   - Suggest concrete next steps

## Fallback (No MCP Server)

If the MCP server is not available, delegate to the matching agent:

- `ouroboros:contrarian` - "What if we're solving the wrong problem?"
- `ouroboros:hacker` - "Make it work, elegance comes later"
- `ouroboros:simplifier` - "Cut scope to the absolute minimum"
- `ouroboros:researcher` - "Stop coding. Read the docs."
- `ouroboros:architect` - "Question the foundation. Rebuild if needed."

These agents use prompt-based lateral thinking without numerical analysis.

## Example

```
User: I'm stuck on the database schema design

/ouroboros:unstuck simplifier

# Lateral Thinking: Reduce to Minimum Viable Schema

Start with exactly 2 tables. If you can't build the core feature
with 2 tables, you haven't found the core feature yet.

## Questions to Consider
- What is the ONE query your users will run most?
- Can you use a single JSON column instead of normalized tables?
- What if you started with flat files and added a DB later?
```
