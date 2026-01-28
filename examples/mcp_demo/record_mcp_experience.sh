#!/bin/bash
# Record MCP User Experience Demo

set -e

echo "============================================"
echo "MCP User Experience Recording"
echo "============================================"
echo ""
echo "This will record:"
echo "  • User interaction with Claude Desktop"
echo "  • Real-time MCP progress visualization"
echo "  • Complete end-to-end workflow"
echo ""
echo "Press Enter to start recording..."
echo ""
sleep 1

# Start asciinema recording
asciinema rec mcp_user_experience.cast \
    --overwrite \
    --title "Ouroboros MCP - User Experience Demo" \
    --command "bash demo_mcp_experience.sh"

echo ""
echo "============================================"
echo "Recording saved to: mcp_user_experience.cast"
echo "============================================"
echo ""
echo "To replay: asciinema play mcp_user_experience.cast"
echo "To upload: asciinema upload mcp_user_experience.cast"
echo ""
