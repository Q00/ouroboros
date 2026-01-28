#!/bin/bash
# MCP User Experience Demo - Simulated Split Screen

set -e

# Colors for simulation
CLAUDE_COLOR="\033[36m"  # Cyan for Claude
USER_COLOR="\033[33m"    # Yellow for User
SYSTEM_COLOR="\033[90m"  # Gray for system
RESET="\033[0m"

clear

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                    MCP User Experience Demo"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "This demo shows:"
echo "  • Left side: User interaction with Claude Desktop"
echo "  • Right side: Real-time MCP progress in terminal"
echo ""
echo "Press Enter to start..."
echo ""
sleep 2

clear

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                         Claude Desktop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

sleep 1

echo -e "${USER_COLOR}👤 User:${RESET}"
echo "   Create a simple hello world Python script that prints a greeting."
echo ""

sleep 2

echo -e "${CLAUDE_COLOR}🤖 Claude:${RESET}"
echo "   I'll use the Ouroboros MCP tool to create this for you."
echo ""

sleep 2

echo -e "${SYSTEM_COLOR}   [Using tool: execute_seed]${RESET}"
echo ""

sleep 1

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                    MCP Server Terminal (Real-time)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

sleep 1

# Execute the actual ouroboros workflow with progress display
~/.local/bin/uv run ouroboros run workflow --orchestrator demo_seed.yaml

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                    Back to Claude Desktop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

sleep 1

echo -e "${CLAUDE_COLOR}🤖 Claude:${RESET}"
echo "   ✅ Done! I've created a hello world Python script at /tmp/hello.py"
echo ""
echo "   The script:"
echo "   • Has proper shebang line (#!/usr/bin/env python3)"
echo "   • Is executable (chmod +x)"
echo "   • Prints 'Hello, World!'"
echo "   • All 4 acceptance criteria completed in 35 seconds"
echo ""

sleep 2

echo -e "${USER_COLOR}👤 User:${RESET}"
echo "   Perfect! Can you show me the file?"
echo ""

sleep 1

echo -e "${CLAUDE_COLOR}🤖 Claude:${RESET}"
cat /tmp/hello.py

echo ""
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                         Demo Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Key features shown:"
echo "  ✓ User asks Claude for a task"
echo "  ✓ Claude uses MCP execute_seed tool"
echo "  ✓ Real-time progress display in terminal"
echo "  ✓ AC status tracking: ⏳ → 🔄 → ✅"
echo "  ✓ Results returned to Claude"
echo "  ✓ User sees completed work"
echo ""
