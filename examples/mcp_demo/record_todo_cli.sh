#!/bin/bash
# Record MCP TODO CLI Demo

set -e

echo "============================================"
echo "MCP TODO CLI Demo Recording"
echo "============================================"
echo ""
echo "This will record:"
echo "  • User requesting TODO CLI app from Claude"
echo "  • Real-time MCP progress visualization"
echo "  • Complete workflow with 6 acceptance criteria"
echo ""
echo "Estimated duration: 2-3 minutes"
echo ""
echo "Press Enter to start recording..."
echo ""
sleep 1

# Start asciinema recording
asciinema rec todo_cli_demo.cast \
    --overwrite \
    --title "Ouroboros MCP - TODO CLI Application Demo" \
    --command "bash examples/mcp_demo/demo_todo_cli.sh"

echo ""
echo "============================================"
echo "Recording saved to: todo_cli_demo.cast"
echo "============================================"
echo ""
echo "To replay: asciinema play todo_cli_demo.cast"
echo "To upload: asciinema upload todo_cli_demo.cast"
echo ""
