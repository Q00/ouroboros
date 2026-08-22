"""Execution module — deprecated.

Historical execution strategies (double_diamond, decomposition, atomicity)
were removed after confirming no live callers. The execution engine now
lives in ouroboros.orchestrator and ouroboros.mcp.tools.

See tests/unit/orchestrator/test_decomposition_live_path.py for the
architecture guard that prevents resurrection of the dead modules.
"""
