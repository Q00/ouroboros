"""Streamlit Dashboard for Ouroboros HOTL Convergence Visualization.

This is the main Streamlit application providing:
1. Convergence Curve: % criteria satisfaction over time
2. Pattern Network: Visualization of failure pattern connections
3. Dependency Tree: AC blocking relationship visualization
4. Real-time HOTL Status: Current convergence metrics

Run with: streamlit run app.py

Design Philosophy:
- Philosophy-First: Visualize Socratic questioning and Ontological analysis
- HOTL Convergence: Show acceleration toward solution
- Three Wow Moments: Mind-Reading Interview, Living Tree, Aha Root Cause
"""

import streamlit as st
import json
import pandas as pd
from datetime import datetime, timedelta
import plotly.express as px
import plotly.graph_objects as go
from typing import Any

# Page configuration
st.set_page_config(
    page_title="Ouroboros HOTL Convergence",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #4A90A4;
        margin-bottom: 1rem;
    }
    .metric-card {
        background-color: #f8f9fa;
        border-radius: 10px;
        padding: 1rem;
        margin: 0.5rem 0;
    }
    .status-converged {
        color: #28a745;
        font-weight: bold;
    }
    .status-stagnant {
        color: #dc3545;
        font-weight: bold;
    }
    .status-converging {
        color: #17a2b8;
        font-weight: bold;
    }
    .wow-moment {
        background: linear-gradient(135deg, #4A90A4 0%, #7B68EE 100%);
        color: white;
        padding: 1rem;
        border-radius: 10px;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)


# =============================================================================
# Sample Data Generation (for demo)
# =============================================================================

def generate_demo_data() -> dict[str, Any]:
    """Generate comprehensive demo data for visualization."""
    base_time = datetime.now() - timedelta(hours=2)

    # Generate convergence curve data
    curve_data = []
    satisfaction = 0
    for i in range(1, 76):
        # Simulate realistic convergence with occasional setbacks
        if i < 10:
            satisfaction += 2.5  # Fast initial progress
        elif i < 30:
            satisfaction += 1.2  # Steady progress
        elif i < 50:
            satisfaction += 0.8 + (0.5 if i % 7 == 0 else 0)  # Slower with occasional jumps
        else:
            satisfaction += 0.3 + (1.0 if i % 5 == 0 else 0)  # Final push

        # Add some noise and occasional setbacks
        if i % 15 == 0:
            satisfaction -= 3  # Occasional setback
        satisfaction = max(0, min(95.5, satisfaction))

        outcome = "success" if i % 4 == 0 else ("partial" if i % 3 == 0 else "failure")
        if satisfaction > 90:
            outcome = "success" if i % 2 == 0 else "partial"

        curve_data.append({
            "iteration": i,
            "satisfaction": round(satisfaction, 1),
            "timestamp": (base_time + timedelta(minutes=i * 2)).isoformat(),
            "ac_id": f"AC_{(i % 8) + 1}",
            "outcome": outcome,
            "cumulative_successes": sum(1 for d in curve_data if d.get("outcome") == "success") + (1 if outcome == "success" else 0),
            "cumulative_failures": sum(1 for d in curve_data if d.get("outcome") == "failure") + (1 if outcome == "failure" else 0),
        })

    # Generate pattern data
    patterns = [
        {
            "id": "spinning_abc123",
            "category": "spinning",
            "severity": "high",
            "description": "Import error repeated 5 times",
            "occurrences": 5,
            "affected_acs": ["AC_1", "AC_3"],
            "hypothesis": "Missing module in requirements.txt",
        },
        {
            "id": "oscillation_def456",
            "category": "oscillation",
            "severity": "high",
            "description": "Type error alternating with value error",
            "occurrences": 4,
            "affected_acs": ["AC_2"],
            "hypothesis": "Contradictory type hints",
        },
        {
            "id": "stagnation_ghi789",
            "category": "stagnation",
            "severity": "medium",
            "description": "No progress on AC_5 for 8 iterations",
            "occurrences": 8,
            "affected_acs": ["AC_5"],
            "hypothesis": "Task requires different approach",
        },
        {
            "id": "dependency_jkl012",
            "category": "dependency",
            "severity": "critical",
            "description": "AC_7 blocked by AC_2",
            "occurrences": 3,
            "affected_acs": ["AC_7", "AC_2"],
            "hypothesis": "Ordering issue in task execution",
        },
        {
            "id": "error_mno345",
            "category": "symptom",
            "severity": "medium",
            "description": "Assertion errors in tests",
            "occurrences": 6,
            "affected_acs": ["AC_4", "AC_6"],
            "hypothesis": "Implementation doesn't match test expectations",
        },
    ]

    # Generate network data
    network_nodes = []
    network_edges = []

    # Add pattern nodes
    for p in patterns:
        network_nodes.append({
            "id": p["id"],
            "label": p["description"][:30] + "...",
            "type": "pattern",
            "severity": p["severity"],
            "occurrences": p["occurrences"],
        })

        # Add edges to ACs
        for ac in p["affected_acs"]:
            ac_node_id = f"ac_{ac}"
            if ac_node_id not in [n["id"] for n in network_nodes]:
                network_nodes.append({
                    "id": ac_node_id,
                    "label": ac,
                    "type": "ac",
                })
            network_edges.append({
                "source": p["id"],
                "target": ac_node_id,
                "type": "affects",
                "weight": p["occurrences"],
            })

    # Add inter-pattern relationships
    network_edges.append({
        "source": "spinning_abc123",
        "target": "dependency_jkl012",
        "type": "related",
        "weight": 2,
    })

    # Generate dependency tree
    dependency_tree = {
        "ac_id": "ROOT",
        "depth": 0,
        "is_satisfied": False,
        "is_blocked": False,
        "blocker_count": 0,
        "children": [
            {
                "ac_id": "AC_1",
                "depth": 1,
                "is_satisfied": True,
                "is_blocked": False,
                "blocker_count": 0,
                "children": [
                    {"ac_id": "AC_3", "depth": 2, "is_satisfied": True, "is_blocked": False, "blocker_count": 0, "children": []},
                    {"ac_id": "AC_4", "depth": 2, "is_satisfied": True, "is_blocked": False, "blocker_count": 0, "children": []},
                ],
            },
            {
                "ac_id": "AC_2",
                "depth": 1,
                "is_satisfied": True,
                "is_blocked": False,
                "blocker_count": 0,
                "children": [
                    {"ac_id": "AC_5", "depth": 2, "is_satisfied": False, "is_blocked": True, "blocker_count": 1, "children": []},
                    {
                        "ac_id": "AC_6",
                        "depth": 2,
                        "is_satisfied": True,
                        "is_blocked": False,
                        "blocker_count": 0,
                        "children": [
                            {"ac_id": "AC_7", "depth": 3, "is_satisfied": False, "is_blocked": True, "blocker_count": 2, "children": []},
                        ],
                    },
                ],
            },
            {
                "ac_id": "AC_8",
                "depth": 1,
                "is_satisfied": False,
                "is_blocked": False,
                "blocker_count": 0,
                "children": [],
            },
        ],
    }

    return {
        "curve_data": curve_data,
        "patterns": patterns,
        "network": {"nodes": network_nodes, "edges": network_edges},
        "dependency_tree": dependency_tree,
        "current_state": {
            "total_iterations": 75,
            "successful_iterations": 19,
            "failed_iterations": 32,
            "satisfaction_percentage": 95.5,
            "convergence_rate": 0.25,
            "is_converging": True,
            "is_stagnant": False,
            "context_utilization": 0.42,
        },
    }


# =============================================================================
# Main Dashboard
# =============================================================================

def main():
    """Main dashboard entry point."""
    # Sidebar
    st.sidebar.markdown("## Ouroboros HOTL")
    st.sidebar.markdown("### Philosophy-First AI Quality")

    # Mode selection
    mode = st.sidebar.radio(
        "View Mode",
        ["Live Dashboard", "Demo Mode", "Wow Moments"],
        index=1,
    )

    # Generate or load data
    data = generate_demo_data()

    # Main content area
    st.markdown('<h1 class="main-header"> HOTL Convergence Dashboard</h1>', unsafe_allow_html=True)

    if mode == "Wow Moments":
        render_wow_moments(data)
    elif mode == "Demo Mode":
        render_full_dashboard(data)
    else:
        render_live_dashboard(data)


def render_wow_moments(data: dict[str, Any]):
    """Render the three wow moments for demo."""
    st.markdown("## Three Wow Moments")

    # Wow Moment 1: Mind-Reading Interview
    with st.expander(" Wow Moment 1: Mind-Reading Interview", expanded=True):
        st.markdown("""
        <div class="wow-moment">
        <h3> Socratic Questioning in Action</h3>
        <p>Watch how Ouroboros extracts the <strong>true intent</strong> from vague requirements through iterative questioning.</p>
        </div>
        """, unsafe_allow_html=True)

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### Vague Request")
            st.info('"Make the app faster"')

        with col2:
            st.markdown("### Extracted Intent")
            st.success("""
            **Quantified Requirements:**
            - Page load time < 2s
            - API response time < 200ms
            - Memory usage < 512MB

            **Root Cause Identified:**
            N+1 database queries in product listing
            """)

        # Show the questioning process
        st.markdown("### Socratic Dialogue")
        questions = [
            ("Ouroboros", "What does 'faster' mean to you? Is it page load, API response, or perceived performance?"),
            ("User", "Mostly the product listing page is slow"),
            ("Ouroboros", "When you say 'slow', what time range are we talking about? Current vs expected?"),
            ("User", "It takes 5 seconds, should be under 2"),
            ("Ouroboros", "I notice the listing makes 150 DB queries per page. Is this the root cause we should address?"),
            ("User", "Yes! I didn't know that"),
        ]

        for speaker, text in questions:
            if speaker == "Ouroboros":
                st.markdown(f"** {speaker}:** {text}")
            else:
                st.markdown(f"** {speaker}:** {text}")

    # Wow Moment 2: Living Tree
    st.divider()
    with st.expander(" Wow Moment 2: Living Tree", expanded=True):
        st.markdown("""
        <div class="wow-moment">
        <h3> Convergence Visualization</h3>
        <p>Watch the AC tree come alive as criteria are satisfied, showing real-time HOTL convergence.</p>
        </div>
        """, unsafe_allow_html=True)

        render_convergence_visualization(data)

    # Wow Moment 3: Aha Root Cause
    st.divider()
    with st.expander(" Wow Moment 3: Aha Root Cause", expanded=True):
        st.markdown("""
        <div class="wow-moment">
        <h3> Ontological Analysis</h3>
        <p>Watch Gemini 3 identify the <strong>essential nature</strong> of problems, distinguishing root causes from symptoms.</p>
        </div>
        """, unsafe_allow_html=True)

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### Symptom Chain")
            st.error("""
            **Iteration 47:** ImportError: No module named 'utils'
            **Iteration 48:** ImportError: No module named 'utils'
            **Iteration 49:** ImportError: No module named 'utils'
            **Pattern Detected:** SPINNING (3x)
            """)

        with col2:
            st.markdown("### Root Cause Analysis")
            st.success("""
            ** Gemini 3 Analysis:**

            "The repeated ImportError is not the root cause. The essential
            problem is that 'utils' was renamed to 'helpers' in AC_2, but
            AC_4 still references the old name.

            **Root Cause:** Module refactoring incomplete
            **Solution:** Update imports in AC_4 to use 'helpers'"
            """)

        # Show Devil's Advocate challenge
        st.markdown("### Devil's Advocate Challenge")
        st.warning("""
        ** Devil's Advocate asks:**

        "Are we treating the symptom or the cause? If we just rename the import,
        will the same issue recur when another module is renamed?

        **Deeper question:** Is there a missing abstraction layer that should
        protect consumers from implementation changes?"
        """)


def render_full_dashboard(data: dict[str, Any]):
    """Render the full dashboard with all visualizations."""
    # Status metrics row
    render_status_metrics(data)

    st.divider()

    # Main visualizations in tabs
    tab1, tab2, tab3, tab4 = st.tabs([
        " Convergence Curve",
        " Pattern Network",
        " Dependency Tree",
        " Pattern Analysis"
    ])

    with tab1:
        render_convergence_visualization(data)

    with tab2:
        render_pattern_network_visualization(data)

    with tab3:
        render_dependency_tree_visualization(data)

    with tab4:
        render_pattern_analysis(data)


def render_live_dashboard(data: dict[str, Any]):
    """Render live monitoring dashboard."""
    st.info(" Live mode would connect to running Ouroboros instance. Showing demo data.")
    render_full_dashboard(data)


def render_status_metrics(data: dict[str, Any]):
    """Render status metrics row."""
    state = data["current_state"]

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.metric(
            "Satisfaction",
            f"{state['satisfaction_percentage']:.1f}%",
            delta="+2.3%",
        )

    with col2:
        st.metric(
            "Total Iterations",
            state["total_iterations"],
        )

    with col3:
        st.metric(
            "Successes",
            state["successful_iterations"],
            delta="+3",
        )

    with col4:
        st.metric(
            "Convergence Rate",
            f"{state['convergence_rate']:.1%}",
        )

    with col5:
        st.metric(
            "Context Used",
            f"{state['context_utilization']:.0%}",
            help="1M token context utilization",
        )

    # Status indicator
    if state["satisfaction_percentage"] >= 95:
        st.success(" CONVERGED - All criteria satisfied!")
    elif state["is_stagnant"]:
        st.error(" STAGNANT - No progress detected")
    elif state["is_converging"]:
        st.info(" CONVERGING - Making progress...")
    else:
        st.warning(" STRUGGLING - May need intervention")


def render_convergence_visualization(data: dict[str, Any]):
    """Render convergence curve visualization."""
    st.markdown("### Convergence Curve")
    st.caption("Track % criteria satisfaction over HOTL iterations")

    df = pd.DataFrame(data["curve_data"])

    # Main line chart
    fig = go.Figure()

    # Add satisfaction line
    fig.add_trace(go.Scatter(
        x=df["iteration"],
        y=df["satisfaction"],
        mode="lines",
        name="Satisfaction %",
        line=dict(color="#4A90A4", width=3),
        fill="tozeroy",
        fillcolor="rgba(74, 144, 164, 0.1)",
    ))

    # Add success markers
    success_df = df[df["outcome"] == "success"]
    fig.add_trace(go.Scatter(
        x=success_df["iteration"],
        y=success_df["satisfaction"],
        mode="markers",
        name="Success",
        marker=dict(color="#28a745", size=10, symbol="circle"),
    ))

    # Add failure markers
    failure_df = df[df["outcome"] == "failure"]
    fig.add_trace(go.Scatter(
        x=failure_df["iteration"],
        y=failure_df["satisfaction"],
        mode="markers",
        name="Failure",
        marker=dict(color="#dc3545", size=8, symbol="x"),
    ))

    # Add threshold line
    fig.add_hline(
        y=95,
        line_dash="dash",
        line_color="#ffc107",
        annotation_text="Convergence Threshold (95%)",
    )

    fig.update_layout(
        height=400,
        xaxis_title="Iteration",
        yaxis_title="Satisfaction (%)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
    )

    st.plotly_chart(fig, use_container_width=True)

    # Show cumulative stats
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Peak Satisfaction", f"{df['satisfaction'].max():.1f}%")
    with col2:
        st.metric("Avg Progress/Iteration", f"{df['satisfaction'].diff().mean():.2f}%")
    with col3:
        st.metric("Time to 90%", f"{len(df[df['satisfaction'] < 90])} iterations")


def render_pattern_network_visualization(data: dict[str, Any]):
    """Render pattern network graph."""
    st.markdown("### Failure Pattern Network")
    st.caption("Visualize connections between failure patterns and affected ACs")

    network = data["network"]

    # Create networkx-style visualization with Plotly
    import math

    nodes = network["nodes"]
    edges = network["edges"]

    # Calculate positions using circular layout
    n_nodes = len(nodes)
    positions = {}
    for i, node in enumerate(nodes):
        angle = 2 * math.pi * i / n_nodes
        positions[node["id"]] = (math.cos(angle), math.sin(angle))

    # Create figure
    fig = go.Figure()

    # Add edges
    for edge in edges:
        x0, y0 = positions[edge["source"]]
        x1, y1 = positions[edge["target"]]

        color = {
            "affects": "#6c757d",
            "related": "#17a2b8",
            "causes": "#dc3545",
        }.get(edge["type"], "#adb5bd")

        fig.add_trace(go.Scatter(
            x=[x0, x1, None],
            y=[y0, y1, None],
            mode="lines",
            line=dict(width=edge.get("weight", 1), color=color),
            hoverinfo="none",
            showlegend=False,
        ))

    # Add nodes
    for node in nodes:
        x, y = positions[node["id"]]

        color = "#17a2b8" if node["type"] == "ac" else {
            "critical": "#dc3545",
            "high": "#fd7e14",
            "medium": "#ffc107",
            "low": "#28a745",
        }.get(node.get("severity"), "#6c757d")

        size = 30 if node["type"] == "ac" else 20 + node.get("occurrences", 1) * 5

        fig.add_trace(go.Scatter(
            x=[x],
            y=[y],
            mode="markers+text",
            marker=dict(size=size, color=color),
            text=[node["label"]],
            textposition="bottom center",
            name=node["id"],
            hovertemplate=f"<b>{node['label']}</b><br>Type: {node['type']}<extra></extra>",
        ))

    fig.update_layout(
        height=500,
        showlegend=False,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        hovermode="closest",
    )

    st.plotly_chart(fig, use_container_width=True)

    # Legend
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(" **Critical Pattern**")
    with col2:
        st.markdown(" **High Severity**")
    with col3:
        st.markdown(" **Medium Severity**")
    with col4:
        st.markdown(" **AC Node**")


def render_dependency_tree_visualization(data: dict[str, Any]):
    """Render dependency tree."""
    st.markdown("### AC Dependency Tree")
    st.caption("Visualize which ACs block others from completion")

    tree = data["dependency_tree"]

    # Flatten tree for display
    def flatten_tree(node, level=0, parent_line=""):
        lines = []
        prefix = "  " * level
        icon = "" if node["is_satisfied"] else ("" if node["is_blocked"] else "")

        if node["ac_id"] != "ROOT":
            status = "satisfied" if node["is_satisfied"] else ("blocked" if node["is_blocked"] else "pending")
            lines.append({
                "level": level,
                "ac_id": node["ac_id"],
                "status": status,
                "icon": icon,
                "display": f"{prefix}{icon} {node['ac_id']}",
            })

        for child in node.get("children", []):
            lines.extend(flatten_tree(child, level + 1))

        return lines

    flat_tree = flatten_tree(tree)

    # Display as expandable tree
    df = pd.DataFrame(flat_tree)

    for _, row in df.iterrows():
        status_color = {
            "satisfied": "green",
            "blocked": "red",
            "pending": "orange",
        }.get(row["status"], "gray")

        st.markdown(
            f"{'&nbsp;' * row['level'] * 4}"
            f"<span style='color: {status_color}'>{row['icon']}</span> "
            f"**{row['ac_id']}**",
            unsafe_allow_html=True,
        )

    # Summary stats
    st.divider()
    col1, col2, col3 = st.columns(3)
    with col1:
        satisfied = len([r for r in flat_tree if r["status"] == "satisfied"])
        st.metric("Satisfied", satisfied, help="ACs completed successfully")
    with col2:
        blocked = len([r for r in flat_tree if r["status"] == "blocked"])
        st.metric("Blocked", blocked, help="ACs waiting on dependencies")
    with col3:
        pending = len([r for r in flat_tree if r["status"] == "pending"])
        st.metric("Pending", pending, help="ACs ready to work on")


def render_pattern_analysis(data: dict[str, Any]):
    """Render detailed pattern analysis."""
    st.markdown("### Detected Patterns")

    patterns = data["patterns"]

    for pattern in patterns:
        severity_colors = {
            "critical": "#dc3545",
            "high": "#fd7e14",
            "medium": "#ffc107",
            "low": "#28a745",
        }
        color = severity_colors.get(pattern["severity"], "#6c757d")

        with st.expander(f"{pattern['category'].upper()}: {pattern['description']}", expanded=False):
            col1, col2 = st.columns([2, 1])

            with col1:
                st.markdown(f"**Severity:** :{pattern['severity']}_square: {pattern['severity'].upper()}")
                st.markdown(f"**Occurrences:** {pattern['occurrences']}")
                st.markdown(f"**Affected ACs:** {', '.join(pattern['affected_acs'])}")

            with col2:
                st.info(f" **Root Cause Hypothesis:**\n{pattern['hypothesis']}")

            # Socratic questions
            st.markdown("**Socratic Questions:**")
            questions = {
                "spinning": ["Why is the same error repeating?", "What context is missing?"],
                "oscillation": ["What causes the flip-flopping?", "Are the fixes contradictory?"],
                "stagnation": ["Why is no progress being made?", "Is human guidance needed?"],
                "dependency": ["What must be completed first?", "Can the dependency be removed?"],
                "symptom": ["Is this the root cause or a symptom?", "What deeper issue exists?"],
            }
            for q in questions.get(pattern["category"], []):
                st.markdown(f"- {q}")


if __name__ == "__main__":
    main()
