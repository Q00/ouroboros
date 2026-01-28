#!/bin/bash
# MCP User Experience Demo - TODO CLI Application

set -e

# Colors for simulation
CLAUDE_COLOR="\033[36m"  # Cyan for Claude
USER_COLOR="\033[33m"    # Yellow for User
SYSTEM_COLOR="\033[90m"  # Gray for system
RESET="\033[0m"

clear

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                MCP User Experience Demo - TODO CLI"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "This demo shows how users interact with Claude Desktop using MCP tools"
echo "to build a complete TODO CLI application with persistent storage."
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
echo "   Create a command-line TODO list application with these features:"
echo "   • Add new tasks"
echo "   • List all tasks"
echo "   • Mark tasks as complete"
echo "   • Persistent storage in JSON"
echo "   • Include tests"
echo ""

sleep 3

echo -e "${CLAUDE_COLOR}🤖 Claude:${RESET}"
echo "   I'll use the Ouroboros MCP tool to build this for you."
echo "   This will create a complete CLI application with 6 acceptance criteria."
echo ""

sleep 2

echo -e "${SYSTEM_COLOR}   [Using tool: execute_seed]${RESET}"
echo ""

sleep 1

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                 MCP Server Terminal (Real-time Progress)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

sleep 1

# Execute the actual ouroboros workflow with progress display
~/.local/bin/uv run ouroboros run workflow --orchestrator examples/mcp_demo/todo_cli_seed.yaml

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                    Back to Claude Desktop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

sleep 1

echo -e "${CLAUDE_COLOR}🤖 Claude:${RESET}"
echo "   ✅ Done! I've created a complete TODO CLI application."
echo ""
echo "   What was built:"
echo "   • todo.py - CLI with add/list/complete commands"
echo "   • JSON-based persistent storage"
echo "   • Comprehensive error handling"
echo "   • Test suite covering all functionality"
echo "   • All 6 acceptance criteria completed"
echo ""

sleep 2

echo -e "${USER_COLOR}👤 User:${RESET}"
echo "   Can you show me how to use it?"
echo ""

sleep 1

echo -e "${CLAUDE_COLOR}🤖 Claude:${RESET}"
echo "   Sure! Here are some example commands:"
echo ""
cat << 'EOF'
   # Add a task
   python3 todo.py add "Buy groceries"

   # List all tasks
   python3 todo.py list

   # Complete a task
   python3 todo.py complete 1

   # Run tests
   python3 test_todo.py
EOF

echo ""
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "                         Demo Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Key features demonstrated:"
echo "  ✓ User requests a complex feature from Claude"
echo "  ✓ Claude uses MCP execute_seed tool"
echo "  ✓ Real-time progress display in MCP server terminal"
echo "  ✓ AC status tracking: ⏳ → 🔄 → ✅"
echo "  ✓ Complete application with tests delivered"
echo ""
echo "This shows how MCP enables Claude to execute complex workflows"
echo "while providing real-time visibility into progress."
echo ""
