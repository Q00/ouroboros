"""Streamlit Dashboard for HOTL Convergence Visualization.

This package provides Streamlit-based visualization components:
1. Convergence curve visualization
2. Pattern network graph
3. Dependency tree visualization
4. Real-time HOTL status dashboard

Usage:
    streamlit run ouroboros/gemini3/dashboard/app.py
"""

from ouroboros.gemini3.dashboard.visualizations import (
    render_convergence_curve,
    render_pattern_network,
    render_dependency_tree,
    render_hotl_status,
)

__all__ = [
    "render_convergence_curve",
    "render_pattern_network",
    "render_dependency_tree",
    "render_hotl_status",
]
