"""Visualization Components for HOTL Dashboard.

This module provides Streamlit visualization components for:
1. Convergence curve: % criteria satisfaction over time
2. Pattern network: Connections between failed iterations
3. Dependency tree: AC blocking relationships

Design Philosophy:
- Clear visual representation of HOTL progress
- Interactive exploration of failure patterns
- Real-time convergence monitoring
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.gemini3.convergence_accelerator import (
        ConvergenceCurvePoint,
        ConvergenceState,
    )
    from ouroboros.gemini3.pattern_analyzer import PatternNetwork, FailurePattern
    from ouroboros.gemini3.dependency_predictor import DependencyTreeNode


# =============================================================================
# Convergence Curve Visualization
# =============================================================================


def render_convergence_curve(
    curve_points: list[ConvergenceCurvePoint],
    title: str = "HOTL Convergence Curve",
) -> dict[str, Any]:
    """Render convergence curve data for Streamlit visualization.

    Shows % criteria satisfaction over iterations, with markers for
    successes and failures.

    Args:
        curve_points: List of convergence curve points
        title: Chart title

    Returns:
        Dictionary with chart data for Streamlit
    """
    if not curve_points:
        return {
            "title": title,
            "data": [],
            "annotations": [],
            "metrics": {
                "current_satisfaction": 0,
                "total_iterations": 0,
                "trend": "neutral",
            },
        }

    # Prepare data series
    iterations = [p.iteration_number for p in curve_points]
    satisfaction = [p.satisfaction_percentage for p in curve_points]

    # Calculate success/failure markers
    successes = [
        {"x": p.iteration_number, "y": p.satisfaction_percentage}
        for p in curve_points
        if p.outcome.value == "success"
    ]
    failures = [
        {"x": p.iteration_number, "y": p.satisfaction_percentage}
        for p in curve_points
        if p.outcome.value in ("failure", "stagnant")
    ]

    # Calculate trend
    if len(satisfaction) >= 2:
        recent_trend = satisfaction[-1] - satisfaction[-min(5, len(satisfaction))]
        if recent_trend > 5:
            trend = "improving"
        elif recent_trend < -5:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = "insufficient_data"

    # Prepare chart data for Streamlit/Plotly
    chart_data = {
        "title": title,
        "x_axis": "Iteration",
        "y_axis": "Satisfaction (%)",
        "main_series": {
            "name": "Satisfaction",
            "x": iterations,
            "y": satisfaction,
            "type": "line",
            "color": "#4A90A4",
        },
        "success_markers": {
            "name": "Success",
            "data": successes,
            "color": "#28a745",
            "symbol": "circle",
        },
        "failure_markers": {
            "name": "Failure",
            "data": failures,
            "color": "#dc3545",
            "symbol": "x",
        },
        "threshold_line": {
            "y": 95,
            "label": "Convergence Threshold (95%)",
            "color": "#ffc107",
            "dash": "dash",
        },
        "metrics": {
            "current_satisfaction": satisfaction[-1] if satisfaction else 0,
            "total_iterations": len(curve_points),
            "trend": trend,
            "successes": len(successes),
            "failures": len(failures),
        },
    }

    return chart_data


def render_convergence_altair_spec(
    curve_points: list[ConvergenceCurvePoint],
) -> str:
    """Generate Altair/Vega-Lite spec for convergence curve.

    Returns JSON spec for embedding in Streamlit.
    """
    data = [
        {
            "iteration": p.iteration_number,
            "satisfaction": p.satisfaction_percentage,
            "outcome": p.outcome.value,
            "ac_id": p.ac_id,
            "timestamp": p.timestamp.isoformat(),
        }
        for p in curve_points
    ]

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "HOTL Convergence Progress",
        "data": {"values": data},
        "width": 600,
        "height": 400,
        "layer": [
            {
                "mark": {"type": "line", "color": "#4A90A4"},
                "encoding": {
                    "x": {"field": "iteration", "type": "quantitative", "title": "Iteration"},
                    "y": {"field": "satisfaction", "type": "quantitative", "title": "Satisfaction (%)"},
                },
            },
            {
                "mark": {"type": "point", "filled": True, "size": 100},
                "encoding": {
                    "x": {"field": "iteration", "type": "quantitative"},
                    "y": {"field": "satisfaction", "type": "quantitative"},
                    "color": {
                        "field": "outcome",
                        "type": "nominal",
                        "scale": {
                            "domain": ["success", "failure", "partial", "stagnant", "blocked"],
                            "range": ["#28a745", "#dc3545", "#ffc107", "#6c757d", "#17a2b8"],
                        },
                    },
                    "tooltip": [
                        {"field": "iteration", "title": "Iteration"},
                        {"field": "satisfaction", "title": "Satisfaction %"},
                        {"field": "outcome", "title": "Outcome"},
                        {"field": "ac_id", "title": "AC"},
                    ],
                },
            },
            {
                "mark": {"type": "rule", "color": "#ffc107", "strokeDash": [4, 4]},
                "encoding": {
                    "y": {"datum": 95},
                },
            },
        ],
    }

    return json.dumps(spec)


# =============================================================================
# Pattern Network Visualization
# =============================================================================


def render_pattern_network(
    network: PatternNetwork,
    title: str = "Failure Pattern Network",
) -> dict[str, Any]:
    """Render pattern network for Streamlit visualization.

    Shows connections between failure patterns and affected ACs.

    Args:
        network: Pattern network with nodes and edges
        title: Chart title

    Returns:
        Dictionary with network data for visualization
    """
    network_dict = network.to_dict()

    # Calculate node sizes and colors
    nodes_with_style = []
    for node in network_dict["nodes"]:
        style = {
            "id": node["id"],
            "label": node["label"],
            "type": node["type"],
            "size": max(10, node.get("weight", 1) * 5),
        }

        if node["type"] == "pattern":
            severity = node.get("severity", "medium")
            style["color"] = {
                "critical": "#dc3545",
                "high": "#fd7e14",
                "medium": "#ffc107",
                "low": "#28a745",
            }.get(severity, "#6c757d")
        else:
            style["color"] = "#17a2b8"  # AC nodes

        nodes_with_style.append(style)

    # Calculate edge weights
    edges_with_style = []
    for edge in network_dict["edges"]:
        style = {
            "source": edge["source"],
            "target": edge["target"],
            "label": edge["label"],
            "width": max(1, edge.get("weight", 1)),
            "color": {
                "affects": "#6c757d",
                "related": "#17a2b8",
                "causes": "#dc3545",
                "blocks": "#fd7e14",
            }.get(edge["type"], "#adb5bd"),
        }
        edges_with_style.append(style)

    return {
        "title": title,
        "nodes": nodes_with_style,
        "edges": edges_with_style,
        "layout": "force-directed",
        "metrics": {
            "total_patterns": len([n for n in nodes_with_style if n["type"] == "pattern"]),
            "total_acs": len([n for n in nodes_with_style if n["type"] == "ac"]),
            "total_connections": len(edges_with_style),
        },
    }


def render_pattern_network_vis_spec(network: PatternNetwork) -> str:
    """Generate vis.js network spec for pattern visualization.

    Returns JSON spec for embedding in Streamlit with streamlit-agraph.
    """
    network_dict = network.to_dict()

    # Transform for vis.js format
    vis_nodes = []
    for node in network_dict["nodes"]:
        vis_node = {
            "id": node["id"],
            "label": node["label"][:30],  # Truncate long labels
            "title": node["label"],  # Full label on hover
            "group": node["type"],
            "value": node.get("weight", 1),
        }

        if node["type"] == "pattern":
            severity = node.get("severity", "medium")
            vis_node["color"] = {
                "critical": {"background": "#dc3545", "border": "#a71d2a"},
                "high": {"background": "#fd7e14", "border": "#d95d00"},
                "medium": {"background": "#ffc107", "border": "#d4a106"},
                "low": {"background": "#28a745", "border": "#1e7e34"},
            }.get(severity, {"background": "#6c757d", "border": "#545b62"})
            vis_node["shape"] = "box"
        else:
            vis_node["color"] = {"background": "#17a2b8", "border": "#138496"}
            vis_node["shape"] = "ellipse"

        vis_nodes.append(vis_node)

    vis_edges = []
    for edge in network_dict["edges"]:
        vis_edge = {
            "from": edge["source"],
            "to": edge["target"],
            "label": edge.get("label", "")[:20],
            "value": edge.get("weight", 1),
            "arrows": "to",
        }

        edge_type = edge.get("type", "related")
        vis_edge["color"] = {
            "affects": "#6c757d",
            "related": "#17a2b8",
            "causes": "#dc3545",
            "blocks": "#fd7e14",
        }.get(edge_type, "#adb5bd")

        if edge_type == "related":
            vis_edge["dashes"] = True

        vis_edges.append(vis_edge)

    return json.dumps({
        "nodes": vis_nodes,
        "edges": vis_edges,
        "options": {
            "physics": {
                "enabled": True,
                "solver": "forceAtlas2Based",
            },
            "interaction": {
                "hover": True,
                "tooltipDelay": 100,
            },
        },
    })


# =============================================================================
# Dependency Tree Visualization
# =============================================================================


def render_dependency_tree(
    root: DependencyTreeNode,
    title: str = "AC Dependency Tree",
) -> dict[str, Any]:
    """Render dependency tree for Streamlit visualization.

    Shows AC blocking relationships as a tree structure.

    Args:
        root: Root node of dependency tree
        title: Chart title

    Returns:
        Dictionary with tree data for visualization
    """
    tree_dict = root.to_dict()

    # Flatten tree for table view
    flat_nodes = []

    def flatten(node: dict, parent_id: str | None = None):
        flat_node = {
            "ac_id": node["ac_id"],
            "parent_id": parent_id,
            "depth": node["depth"],
            "is_satisfied": node["is_satisfied"],
            "is_blocked": node["is_blocked"],
            "blocker_count": node["blocker_count"],
            "status_icon": "" if node["is_satisfied"] else ("" if node["is_blocked"] else ""),
        }
        flat_nodes.append(flat_node)

        for child in node.get("children", []):
            flatten(child, node["ac_id"])

    flatten(tree_dict)

    # Calculate metrics
    total_acs = len([n for n in flat_nodes if n["ac_id"] != "ROOT"])
    satisfied = len([n for n in flat_nodes if n["is_satisfied"]])
    blocked = len([n for n in flat_nodes if n["is_blocked"]])

    return {
        "title": title,
        "tree_structure": tree_dict,
        "flat_nodes": flat_nodes,
        "metrics": {
            "total_acs": total_acs,
            "satisfied": satisfied,
            "blocked": blocked,
            "completion_rate": (satisfied / total_acs * 100) if total_acs > 0 else 0,
        },
    }


def render_dependency_tree_d3_spec(root: DependencyTreeNode) -> str:
    """Generate D3.js tree spec for dependency visualization.

    Returns JSON spec for embedding in Streamlit.
    """

    def transform_node(node: DependencyTreeNode) -> dict:
        """Transform node for D3 tree layout."""
        return {
            "name": node.ac_id,
            "depth": node.depth,
            "satisfied": node.is_satisfied,
            "blocked": node.is_blocked,
            "blocker_count": node.blocker_count,
            "children": [transform_node(c) for c in node.children],
        }

    tree_data = transform_node(root)

    return json.dumps({
        "data": tree_data,
        "config": {
            "nodeSize": [100, 60],
            "separation": 1.5,
            "colorScheme": {
                "satisfied": "#28a745",
                "blocked": "#dc3545",
                "pending": "#ffc107",
            },
        },
    })


# =============================================================================
# HOTL Status Dashboard
# =============================================================================


def render_hotl_status(
    state: ConvergenceState,
    title: str = "HOTL Convergence Status",
) -> dict[str, Any]:
    """Render real-time HOTL status for dashboard.

    Shows key metrics and status indicators.

    Args:
        state: Current convergence state
        title: Dashboard title

    Returns:
        Dictionary with status data for visualization
    """
    # Determine overall status
    if state.satisfaction_percentage >= 95:
        status = "converged"
        status_color = "#28a745"
        status_icon = ""
    elif state.is_stagnant:
        status = "stagnant"
        status_color = "#dc3545"
        status_icon = ""
    elif state.is_converging:
        status = "converging"
        status_color = "#17a2b8"
        status_icon = ""
    else:
        status = "struggling"
        status_color = "#ffc107"
        status_icon = ""

    # Calculate progress bar
    progress = min(100, state.satisfaction_percentage)

    return {
        "title": title,
        "status": {
            "label": status.upper(),
            "color": status_color,
            "icon": status_icon,
        },
        "progress": {
            "value": progress,
            "max": 100,
            "label": f"{progress:.1f}%",
        },
        "metrics": [
            {
                "label": "Total Iterations",
                "value": state.total_iterations,
                "icon": "",
            },
            {
                "label": "Successful",
                "value": state.successful_iterations,
                "icon": "",
                "color": "#28a745",
            },
            {
                "label": "Failed",
                "value": state.failed_iterations,
                "icon": "",
                "color": "#dc3545",
            },
            {
                "label": "Convergence Rate",
                "value": f"{state.convergence_rate:.2%}",
                "icon": "",
            },
            {
                "label": "Context Used",
                "value": f"{state.context_utilization:.1%}",
                "icon": "",
            },
        ],
        "ac_status": [
            {
                "ac_id": ac_id,
                "satisfied": satisfied,
                "icon": "" if satisfied else "",
            }
            for ac_id, satisfied in state.ac_satisfaction.items()
        ],
        "estimated_remaining": state.estimated_remaining,
    }


# =============================================================================
# Utility Functions
# =============================================================================


def generate_sample_data() -> dict[str, Any]:
    """Generate sample data for dashboard testing.

    Returns:
        Dictionary with sample visualization data
    """
    # Sample convergence points
    from datetime import timedelta
    base_time = datetime.now()

    sample_curve = [
        {
            "iteration_number": i,
            "timestamp": (base_time - timedelta(minutes=50 - i)).isoformat(),
            "satisfaction_percentage": min(95, 20 + i * 1.5),
            "ac_id": f"AC_{i % 5 + 1}",
            "outcome": "success" if i % 3 == 0 else "failure",
        }
        for i in range(1, 51)
    ]

    # Sample pattern network
    sample_network = {
        "nodes": [
            {"id": "pattern_1", "type": "pattern", "label": "Import Error Pattern", "weight": 5, "severity": "high"},
            {"id": "pattern_2", "type": "pattern", "label": "Type Error Pattern", "weight": 3, "severity": "medium"},
            {"id": "AC_1", "type": "ac", "label": "AC_1", "weight": 1},
            {"id": "AC_2", "type": "ac", "label": "AC_2", "weight": 1},
        ],
        "edges": [
            {"source": "pattern_1", "target": "AC_1", "type": "affects", "weight": 3},
            {"source": "pattern_1", "target": "AC_2", "type": "affects", "weight": 2},
            {"source": "pattern_2", "target": "AC_1", "type": "affects", "weight": 1},
        ],
    }

    # Sample dependency tree
    sample_tree = {
        "ac_id": "ROOT",
        "depth": 0,
        "is_satisfied": False,
        "is_blocked": False,
        "blocker_count": 0,
        "children": [
            {
                "ac_id": "AC_Base_Setup",
                "depth": 1,
                "is_satisfied": True,
                "is_blocked": False,
                "blocker_count": 0,
                "children": [
                    {
                        "ac_id": "AC_Feature_1",
                        "depth": 2,
                        "is_satisfied": True,
                        "is_blocked": False,
                        "blocker_count": 0,
                        "children": [],
                    },
                    {
                        "ac_id": "AC_Feature_2",
                        "depth": 2,
                        "is_satisfied": False,
                        "is_blocked": True,
                        "blocker_count": 1,
                        "children": [],
                    },
                ],
            },
        ],
    }

    return {
        "curve_data": sample_curve,
        "network_data": sample_network,
        "tree_data": sample_tree,
    }


__all__ = [
    "render_convergence_curve",
    "render_convergence_altair_spec",
    "render_pattern_network",
    "render_pattern_network_vis_spec",
    "render_dependency_tree",
    "render_dependency_tree_d3_spec",
    "render_hotl_status",
    "generate_sample_data",
]
