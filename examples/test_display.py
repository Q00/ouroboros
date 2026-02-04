#!/usr/bin/env python3
"""Test script to simulate workflow display progress.

Run with: uv run python examples/test_display.py
"""

import asyncio
import random
from datetime import UTC, datetime

from ouroboros.cli.formatters.workflow_display import WorkflowDisplay
from ouroboros.orchestrator.workflow_state import (
    ACStatus,
    ActivityType,
    WorkflowStateTracker,
)


async def simulate_workflow() -> None:
    """Simulate a workflow execution with progress updates."""

    # Create tracker with sample acceptance criteria
    tracker = WorkflowStateTracker(
        acceptance_criteria=[
            "Create a hello.py file with a greet() function",
            "Create test_hello.py with at least 2 test cases",
            "All tests must pass when running pytest",
            "Add docstrings to all functions",
            "Create a README.md with usage instructions",
        ],
        goal="Create a simple Python hello world script with tests and documentation",
        session_id=f"sim_{datetime.now(UTC).strftime('%H%M%S')}",
    )

    tools = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]

    print("\n🚀 Starting workflow simulation...\n")
    print("Press Ctrl+C to stop\n")

    with WorkflowDisplay(tracker, refresh_per_second=4) as display:
        current_ac = 1

        for i in range(100):  # Simulate 100 messages
            await asyncio.sleep(0.3)  # Delay between messages

            # Simulate tool usage
            tool = random.choice(tools)
            content = f"Using {tool} to work on the task..."

            # Add AC markers at certain points
            if i == 5:
                content = "[AC_START: 1] Starting to create hello.py..."
            elif i == 15:
                content = "[AC_COMPLETE: 1] Created hello.py with greet() function"
            elif i == 20:
                content = "[AC_START: 2] Now creating test_hello.py..."
            elif i == 35:
                content = "[AC_COMPLETE: 2] Test file created with 2 test cases"
            elif i == 40:
                content = "[AC_START: 3] Running pytest..."
            elif i == 50:
                content = "[AC_COMPLETE: 3] All tests pass!"
            elif i == 55:
                content = "[AC_START: 4] Adding docstrings..."
            elif i == 65:
                content = "[AC_COMPLETE: 4] Docstrings added"
            elif i == 70:
                content = "[AC_START: 5] Creating README..."
            elif i == 85:
                content = "[AC_COMPLETE: 5] README.md created"

            tracker.process_message(
                content=content,
                message_type="assistant",
                tool_name=tool if random.random() > 0.3 else None,
                is_input=False,
            )

            display.refresh()

        # Final message
        print("\n\n✅ Workflow simulation completed!\n")


if __name__ == "__main__":
    try:
        asyncio.run(simulate_workflow())
    except KeyboardInterrupt:
        print("\n\n⛔ Simulation interrupted by user\n")
