#!/usr/bin/env bash
# demo-scene.sh - Demo scene scripts for different demo types
# Each function represents a reusable demo scene

set -e

# Colors for demo output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Pause function for dramatic effect
pause() {
    local duration="${1:-1.5}"
    sleep "$duration"
}

# Print command with prompt
print_cmd() {
    echo -e "${GREEN}$${NC} $1"
    pause
}

# Scene: Quick Start Demo (30 seconds)
scene_quickstart() {
    clear
    echo -e "${BLUE}Ouroboros Quick Start Demo${NC}"
    echo "===================================="
    echo ""
    pause

    echo "Ouroboros is a Claude Code plugin for autonomous workflow execution."
    pause 2

    echo "Let's create a counter component in seconds..."
    pause 2

    print_cmd "ooo run Create a simple counter component with increment/decrement buttons"
    pause

    # Simulate execution (replace with actual ooo run command)
    echo -e "${YELLOW}[Planning phase]${NC} Analyzing requirements..."
    pause 1
    echo -e "${YELLOW}[Planning phase]${NC} Breaking down into tasks..."
    pause 1
    echo -e "${BLUE}[Execution phase]${NC} Creating component structure..."
    pause 1
    echo -e "${BLUE}[Execution phase]${NC} Writing Counter.tsx..."
    pause 1
    echo -e "${BLUE}[Execution phase]${NC} Adding styles..."
    pause 1
    echo -e "${GREEN}[Verification phase]${NC} Testing component..."
    pause 1
    echo -e "${GREEN}[Verification phase]${NC} All tests passing!"
    pause 1

    echo ""
    echo "Done! Component created at:"
    echo "  src/components/Counter.tsx"
    pause

    echo ""
    print_cmd "cat src/components/Counter.tsx"
    pause

    cat << 'EOF'
// Counter component with increment/decrement buttons
import React, { useState } from 'react';

export const Counter = () => {
  const [count, setCount] = useState(0);

  return (
    <div className="counter">
      <button onClick={() => setCount(c => c - 1)}>-</button>
      <span className="count">{count}</span>
      <button onClick={() => setCount(c => c + 1)}>+</button>
    </div>
  );
};
EOF
    pause 2

    echo ""
    echo -e "${GREEN}Demo complete!${NC}"
    echo "Type 'ooo welcome' to get started."
}

# Scene: Next.js App Demo
scene_nextjs() {
    clear
    echo -e "${BLUE}Ouroboros Next.js App Demo${NC}"
    echo "===================================="
    echo ""
    pause

    echo "Creating a modern Next.js app with shadcn/ui..."
    pause 2

    print_cmd "ooo run Create a Next.js 14 app with shadcn/ui and a dark mode toggle"
    pause

    # Simulate execution
    echo -e "${YELLOW}[Planning]${NC} Setting up Next.js 14 project..."
    pause 1
    echo -e "${YELLOW}[Planning]${NC} Configuring shadcn/ui components..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Running: npx create-next-app@14..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Installing shadcn/ui dependencies..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Creating theme provider..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Adding dark mode toggle..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} Starting dev server..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} Checking http://localhost:3000..."
    pause 1

    echo ""
    echo -e "${GREEN}Next.js app ready!${NC}"
    echo ""
    echo "Project structure:"
    echo "  nextjs-app/"
    echo "  ├── app/"
    echo "  │   ├── layout.tsx"
    echo "  │   ├── page.tsx"
    echo "  │   └── theme-provider.tsx"
    echo "  ├── components/"
    echo "  │   └── ui/"
    echo "  └── package.json"
    pause 2

    echo ""
    echo -e "${GREEN}Demo complete!${NC}"
}

# Scene: FastAPI Project Demo
scene_fastapi() {
    clear
    echo -e "${BLUE}Ouroboros FastAPI Project Demo${NC}"
    echo "===================================="
    echo ""
    pause

    echo "Setting up a production-ready FastAPI project..."
    pause 2

    print_cmd "ooo run Create a FastAPI project with PostgreSQL, SQLAlchemy, and pytest"
    pause

    # Simulate execution
    echo -e "${YELLOW}[Planning]${NC} Designing project structure..."
    pause 1
    echo -e "${YELLOW}[Planning]${NC} Setting up database models..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Creating FastAPI project..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Configuring PostgreSQL connection..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Setting up SQLAlchemy models..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Creating API endpoints..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Writing pytest tests..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} Running migrations..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} Running tests..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} All tests passed!"
    pause 1

    echo ""
    echo -e "${GREEN}FastAPI project ready!${NC}"
    echo ""
    echo "Project structure:"
    echo "  fastapi-project/"
    echo "  ├── app/"
    echo "  │   ├── main.py"
    echo "  │   ├── models/"
    echo "  │   ├── routers/"
    echo "  │   └── database.py"
    echo "  ├── tests/"
    echo "  ├── alembic/"
    echo "  └── requirements.txt"
    pause 2

    echo ""
    echo -e "${GREEN}Demo complete!${NC}"
}

# Scene: Debugging Demo
scene_debug() {
    clear
    echo -e "${BLUE}Ouroboros Debugging Demo${NC}"
    echo "===================================="
    echo ""
    pause

    echo "Investigating and fixing a failing test..."
    pause 2

    print_cmd "ooo run Investigate and fix the failing test in tests/api/test_users.py"
    pause

    # Simulate execution
    echo -e "${YELLOW}[Analysis]${NC} Reading test file..."
    pause 1
    echo -e "${YELLOW}[Analysis]${NC} Running tests to see failure..."
    pause 1
    echo -e "${RED}[FAILURE]${NC} test_create_user: AssertionError: Expected 201, got 400"
    pause 1
    echo -e "${YELLOW}[Analysis]${NC} Examining API endpoint..."
    pause 1
    echo -e "${BLUE}[Root Cause]${NC} Missing email validation in request body"
    pause 1
    echo -e "${BLUE}[Fix]${NC} Adding email validation to UserCreate schema..."
    pause 1
    echo -e "${BLUE}[Fix]${NC} Updating test with valid email..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} Running tests again..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} test_create_user PASSED"
    pause 1
    echo -e "${GREEN}[Verification]${NC} All tests PASSED"
    pause 1

    echo ""
    echo -e "${GREEN}Issue resolved!${NC}"
    echo ""
    echo "Changes made:"
    echo "  - Fixed email validation in app/schemas/user.py"
    echo "  - Updated test in tests/api/test_users.py"
    pause 2

    echo ""
    echo -e "${GREEN}Demo complete!${NC}"
}

# Scene: Welcome/Setup Demo
scene_welcome() {
    clear
    echo -e "${BLUE}Ouroboros Setup Demo${NC}"
    echo "===================================="
    echo ""
    pause

    print_cmd "ooo welcome"
    pause

    cat << 'EOF'
Welcome to Ouroboros! 🐍

Ouroboros is a workflow execution engine for Claude Code.

Quick Start:
  ooo run [task description]    Execute a workflow
  ooo status                    Show current workflow status
  ooo help                      Show all commands

Getting Started:
  1. Describe your task: ooo run Create a React counter component
  2. Ouroboros plans, executes, and verifies
  3. Review the results in the output directory

Documentation:
  https://github.com/Q00/ouroboros
EOF
    pause 2

    echo ""
    print_cmd "ooo run Create a simple todo list app"
    pause

    echo -e "${YELLOW}[Planning]${NC} Analyzing requirements..."
    pause 1
    echo -e "${BLUE}[Execution]${NC} Creating todo app..."
    pause 1
    echo -e "${GREEN}[Verification]${NC} Testing functionality..."
    pause 1

    echo ""
    echo -e "${GREEN}Demo complete!${NC}"
}

# Main
main() {
    local scene="${1:-quickstart}"

    case "$scene" in
        quickstart|qs)
            scene_quickstart
            ;;
        nextjs|next)
            scene_nextjs
            ;;
        fastapi|api)
            scene_fastapi
            ;;
        debug|fix)
            scene_debug
            ;;
        welcome|setup)
            scene_welcome
            ;;
        *)
            echo "Unknown scene: $scene"
            echo "Available scenes: quickstart, nextjs, fastapi, debug, welcome"
            exit 1
            ;;
    esac
}

main "$@"
