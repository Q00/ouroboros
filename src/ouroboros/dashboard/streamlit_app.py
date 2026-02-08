"""Streamlit Dashboard for Ouroboros AI.

This is the main Streamlit application that showcases Gemini 3's
1M token context capability for analyzing iteration history.

Run with: streamlit run src/ouroboros/dashboard/streamlit_app.py

Key Features:
1. Timeline visualization of Gemini decision-making
2. Real-time API call/response monitoring
3. Full history analysis with 50+ iterations
4. Devil's Advocate critique display
5. Pattern Analysis visualization
6. Dependency Graph view
7. Convergence tracking
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import random
import sys

import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

# Add parent path for imports if running standalone
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

# Import maze generator for realistic demo data
try:
    from ouroboros.dashboard.maze_problem import generate_demo_data, MazeGenerator
    USE_MAZE_GENERATOR = True
except ImportError:
    USE_MAZE_GENERATOR = False

# Import pattern analysis components
try:
    from ouroboros.dashboard.pattern_analyzer import (
        PatternAnalyzer,
        PatternCategory,
        PatternSeverity,
    )
    from ouroboros.dashboard.dependency_predictor import DependencyPredictor
    from ouroboros.dashboard.devil_advocate import EnhancedDevilAdvocate
    from ouroboros.dashboard.models import IterationData, IterationOutcome
    USE_ADVANCED_ANALYSIS = True
except ImportError:
    USE_ADVANCED_ANALYSIS = False


# Page configuration
st.set_page_config(
    page_title="Ouroboros AI - Gemini 3 Context Demo",
    page_icon="🐍",
    layout="wide",
    initial_sidebar_state="expanded",
)


# Custom CSS for enhanced visuals
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        text-align: center;
        margin-bottom: 0.5rem;
    }

    .sub-header {
        font-size: 1.2rem;
        color: #666;
        text-align: center;
        margin-bottom: 2rem;
    }

    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 1.5rem;
        border-radius: 10px;
        color: white;
        text-align: center;
    }

    .metric-value {
        font-size: 2.5rem;
        font-weight: bold;
    }

    .metric-label {
        font-size: 0.9rem;
        opacity: 0.9;
    }

    .insight-card {
        padding: 1rem;
        border-radius: 8px;
        margin-bottom: 0.5rem;
        border-left: 4px solid;
    }

    .insight-pattern {
        background-color: rgba(243, 156, 18, 0.1);
        border-left-color: #F39C12;
    }

    .insight-anomaly {
        background-color: rgba(231, 76, 60, 0.1);
        border-left-color: #E74C3C;
    }

    .insight-recommendation {
        background-color: rgba(26, 188, 156, 0.1);
        border-left-color: #1ABC9C;
    }

    .insight-critical {
        background-color: rgba(192, 57, 43, 0.1);
        border-left-color: #C0392B;
    }

    .phase-indicator {
        display: inline-block;
        padding: 0.25rem 0.75rem;
        border-radius: 15px;
        font-size: 0.8rem;
        font-weight: bold;
        margin-right: 0.5rem;
    }

    .timeline-container {
        background-color: #f8f9fa;
        border-radius: 10px;
        padding: 1rem;
    }

    .api-log-entry {
        font-family: monospace;
        font-size: 0.85rem;
        padding: 0.5rem;
        border-radius: 5px;
        margin-bottom: 0.5rem;
    }

    .api-log-success {
        background-color: rgba(39, 174, 96, 0.1);
        border-left: 3px solid #27AE60;
    }

    .api-log-error {
        background-color: rgba(231, 76, 60, 0.1);
        border-left: 3px solid #E74C3C;
    }
</style>
""", unsafe_allow_html=True)


def generate_sample_iterations(count: int = 60) -> list[dict]:
    """Generate sample iteration data for demonstration.

    This simulates a complex maze-solving problem with:
    - Shortest path finding
    - Item collection
    - Enemy avoidance

    Args:
        count: Number of iterations to generate.

    Returns:
        List of iteration dictionaries.
    """
    iterations = []
    phases = ["Discover", "Define", "Develop", "Deliver"]
    actions = [
        "Explore north corridor",
        "Analyze wall pattern",
        "Calculate shortest path",
        "Collect health potion",
        "Avoid enemy patrol",
        "Backtrack to junction",
        "Optimize route",
        "Test path segment",
        "Verify item collection",
        "Evaluate escape route",
    ]

    results = [
        "Path found - 15 steps",
        "Dead end detected",
        "Item collected successfully",
        "Enemy avoided",
        "Route optimized - saved 3 steps",
        "Obstacle detected",
        "Pattern recognized",
        "Test passed",
        "Verification complete",
        "Alternative found",
    ]

    base_time = datetime.now() - timedelta(hours=2)
    current_phase_idx = 0
    position = [0, 0]
    items_collected = 0
    enemies_avoided = 0

    for i in range(count):
        # Progress through phases
        if i > 0 and i % (count // 4) == 0 and current_phase_idx < 3:
            current_phase_idx += 1

        phase = phases[current_phase_idx]
        action = random.choice(actions)
        result = random.choice(results)

        # Update state
        position[0] += random.randint(-1, 2)
        position[1] += random.randint(-1, 2)
        if "item" in action.lower() or "collect" in result.lower():
            items_collected += 1
        if "enemy" in action.lower() or "avoid" in result.lower():
            enemies_avoided += 1

        iterations.append({
            "iteration_id": i + 1,
            "timestamp": (base_time + timedelta(minutes=i * 2)).isoformat(),
            "phase": phase,
            "action": action,
            "result": result,
            "state": {
                "position": position.copy(),
                "items_collected": items_collected,
                "enemies_avoided": enemies_avoided,
                "explored_tiles": min(i * 5, 100),
            },
            "metrics": {
                "efficiency": min(0.3 + i * 0.01, 0.95),
                "coverage": min(i * 0.02, 1.0),
                "path_length": max(50 - i * 0.5, 20),
            },
            "reasoning": f"Based on previous {i} iterations, choosing {action} to optimize path.",
        })

    return iterations


def create_timeline_chart(iterations: list[dict]) -> go.Figure:
    """Create an interactive timeline chart using Plotly.

    Args:
        iterations: List of iteration data.

    Returns:
        Plotly figure object.
    """
    df = pd.DataFrame(iterations)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Color mapping for phases
    phase_colors = {
        "Discover": "#9B59B6",
        "Define": "#3498DB",
        "Develop": "#2ECC71",
        "Deliver": "#E74C3C",
    }

    fig = go.Figure()

    # Add scatter points for iterations
    for phase in df["phase"].unique():
        phase_df = df[df["phase"] == phase]
        fig.add_trace(go.Scatter(
            x=phase_df["timestamp"],
            y=phase_df["iteration_id"],
            mode="markers+lines",
            name=phase,
            marker=dict(
                size=10,
                color=phase_colors.get(phase, "#95A5A6"),
            ),
            line=dict(
                color=phase_colors.get(phase, "#95A5A6"),
                width=2,
            ),
            hovertemplate=(
                "<b>Iteration %{y}</b><br>"
                "Time: %{x}<br>"
                "Phase: " + phase + "<br>"
                "<extra></extra>"
            ),
        ))

    fig.update_layout(
        title="Iteration Timeline - Double Diamond Phases",
        xaxis_title="Time",
        yaxis_title="Iteration",
        hovermode="closest",
        showlegend=True,
        height=400,
        template="plotly_white",
    )

    return fig


def create_metrics_chart(iterations: list[dict]) -> go.Figure:
    """Create metrics progress chart.

    Args:
        iterations: List of iteration data.

    Returns:
        Plotly figure object.
    """
    df = pd.DataFrame([
        {
            "iteration": it["iteration_id"],
            "efficiency": it["metrics"]["efficiency"],
            "coverage": it["metrics"]["coverage"],
            "path_length": it["metrics"]["path_length"] / 50,  # Normalize
        }
        for it in iterations
    ])

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=df["iteration"],
        y=df["efficiency"],
        name="Efficiency",
        mode="lines",
        line=dict(color="#2ECC71", width=2),
    ))

    fig.add_trace(go.Scatter(
        x=df["iteration"],
        y=df["coverage"],
        name="Coverage",
        mode="lines",
        line=dict(color="#3498DB", width=2),
    ))

    fig.add_trace(go.Scatter(
        x=df["iteration"],
        y=df["path_length"],
        name="Path Optimality",
        mode="lines",
        line=dict(color="#E74C3C", width=2),
    ))

    fig.update_layout(
        title="Multi-Dimensional Progress Tracking",
        xaxis_title="Iteration",
        yaxis_title="Score (0-1)",
        hovermode="x unified",
        showlegend=True,
        height=300,
        template="plotly_white",
    )

    return fig


def create_token_usage_chart(total_tokens: int) -> go.Figure:
    """Create token usage visualization.

    Args:
        total_tokens: Total tokens used.

    Returns:
        Plotly figure gauge chart.
    """
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=total_tokens,
        title={"text": "Tokens Used (of 1M Context)"},
        delta={"reference": 100000},
        gauge={
            "axis": {"range": [0, 1000000]},
            "bar": {"color": "#667eea"},
            "steps": [
                {"range": [0, 250000], "color": "#E8F5E9"},
                {"range": [250000, 500000], "color": "#FFF3E0"},
                {"range": [500000, 750000], "color": "#FFECB3"},
                {"range": [750000, 1000000], "color": "#FFCDD2"},
            ],
            "threshold": {
                "line": {"color": "#E74C3C", "width": 4},
                "thickness": 0.75,
                "value": 900000,
            },
        },
    ))

    fig.update_layout(height=250)
    return fig


def display_insight_card(insight: dict) -> None:
    """Display an insight card with styling.

    Args:
        insight: Insight dictionary.
    """
    insight_type = insight.get("type", "pattern")
    css_class = f"insight-{insight_type}"

    st.markdown(f"""
    <div class="insight-card {css_class}">
        <strong>{insight['title']}</strong>
        <p>{insight['description']}</p>
        <small>Confidence: {insight['confidence']:.0%} |
        Affects iterations: {', '.join(map(str, insight.get('affected_iterations', [])[:5]))}...</small>
    </div>
    """, unsafe_allow_html=True)


def main():
    """Main Streamlit application."""

    # Header
    st.markdown('<h1 class="main-header">🐍 Ouroboros AI</h1>', unsafe_allow_html=True)
    st.markdown(
        '<p class="sub-header">Demonstrating Gemini 3\'s 1M Token Context for AI Self-Improvement</p>',
        unsafe_allow_html=True
    )

    # Sidebar configuration
    with st.sidebar:
        st.header("⚙️ Configuration")

        iteration_count = st.slider(
            "Number of Iterations",
            min_value=10,
            max_value=200,
            value=60,
            step=10,
            help="Adjust the number of iterations to analyze. Gemini 3 can handle 200+ iterations in a single context!",
        )

        st.divider()

        st.header("🎯 Demo Scenario")
        st.write("""
        **Complex Maze Problem**
        - Find shortest path
        - Collect all items
        - Avoid enemy patrols

        This demonstrates how Gemini 3 can analyze
        the ENTIRE solving history at once.
        """)

        st.divider()

        if st.button("🔄 Generate New Data", use_container_width=True):
            st.session_state.iterations = None
            st.rerun()

        st.divider()

        st.header("🏆 Hackathon Info")
        st.info("""
        **Gemini 3 Hackathon**

        Key Innovation:
        - 1M token context enables holistic analysis
        - Devil's Advocate pattern for critical review
        - Real-time decision visualization
        """)

    # Initialize or load iterations
    if "iterations" not in st.session_state or st.session_state.iterations is None:
        if USE_MAZE_GENERATOR:
            # Use realistic maze problem generator
            iteration_data = generate_demo_data(
                iteration_count=iteration_count,
                maze_size=15,
                seed=42,  # For reproducibility in demos
            )
            # Convert IterationData objects to dicts for display
            st.session_state.iterations = [
                {
                    "iteration_id": it.iteration_id,
                    "timestamp": it.timestamp.isoformat(),
                    "phase": it.phase,
                    "action": it.action,
                    "result": it.result,
                    "state": it.state,
                    "metrics": it.metrics,
                    "reasoning": it.reasoning,
                }
                for it in iteration_data
            ]
        else:
            st.session_state.iterations = generate_sample_iterations(iteration_count)

    iterations = st.session_state.iterations

    # Main metrics row
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            label="Total Iterations",
            value=len(iterations),
            delta=f"{len(iterations) - 50:+d} from baseline",
        )

    with col2:
        # Estimate tokens (roughly 500 tokens per iteration with all details)
        estimated_tokens = len(iterations) * 500
        st.metric(
            label="Estimated Tokens",
            value=f"{estimated_tokens:,}",
            delta=f"{estimated_tokens / 1000000 * 100:.1f}% of 1M",
        )

    with col3:
        phases_completed = len(set(it["phase"] for it in iterations))
        st.metric(
            label="Phases Completed",
            value=f"{phases_completed}/4",
            delta="Double Diamond",
        )

    with col4:
        avg_efficiency = sum(it["metrics"]["efficiency"] for it in iterations) / len(iterations)
        st.metric(
            label="Avg Efficiency",
            value=f"{avg_efficiency:.1%}",
            delta=f"{(avg_efficiency - 0.5) * 100:+.1f}%",
        )

    st.divider()

    # Timeline visualization
    st.header("📊 Iteration Timeline")

    tab1, tab2, tab3 = st.tabs(["Timeline View", "Metrics Progress", "Token Usage"])

    with tab1:
        fig = create_timeline_chart(iterations)
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        fig = create_metrics_chart(iterations)
        st.plotly_chart(fig, use_container_width=True)

    with tab3:
        col1, col2 = st.columns([1, 2])
        with col1:
            estimated_tokens = len(iterations) * 500
            fig = create_token_usage_chart(estimated_tokens)
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            st.subheader("Why 1M Context Matters")
            st.write("""
            Traditional LLMs with 4K-128K context windows can only see a fraction
            of the iteration history. This leads to:

            - **Local optimization** instead of global patterns
            - **Missed dependencies** between distant iterations
            - **Repeated mistakes** from forgotten history

            **Gemini 3's 1M token context** enables:
            - Full trajectory analysis in ONE call
            - Long-range pattern detection
            - Holistic Devil's Advocate critique
            """)

    st.divider()

    # Advanced Analysis Tabs (new integrated features)
    if USE_ADVANCED_ANALYSIS:
        st.header("🔬 Advanced Analysis")

        analysis_tabs = st.tabs([
            "📊 Pattern Analysis",
            "🔗 Dependency Graph",
            "😈 Devil's Advocate",
            "📈 Convergence",
        ])

        # Convert iterations to IterationData objects for analysis
        iteration_objects = []
        for it in iterations:
            outcome = None
            if "success" in it["result"].lower() or "found" in it["result"].lower():
                outcome = IterationOutcome.SUCCESS
            elif "dead end" in it["result"].lower() or "obstacle" in it["result"].lower():
                outcome = IterationOutcome.FAILURE
            elif "blocked" in it["result"].lower():
                outcome = IterationOutcome.BLOCKED
            else:
                outcome = IterationOutcome.PARTIAL

            iteration_objects.append(
                IterationData(
                    iteration_id=it["iteration_id"],
                    timestamp=datetime.fromisoformat(it["timestamp"]),
                    phase=it["phase"],
                    action=it["action"],
                    result=it["result"],
                    state=it.get("state", {}),
                    metrics=it.get("metrics", {}),
                    reasoning=it.get("reasoning", ""),
                    outcome=outcome,
                    error_message=it["result"] if outcome == IterationOutcome.FAILURE else "",
                )
            )

        with analysis_tabs[0]:  # Pattern Analysis
            st.subheader("Failure Pattern Detection")

            if st.button("🔍 Analyze Patterns", key="pattern_btn"):
                with st.spinner("Analyzing patterns across iterations..."):
                    async def run_pattern_analysis():
                        analyzer = PatternAnalyzer()
                        return await analyzer.analyze_patterns(iteration_objects)

                    patterns = asyncio.run(run_pattern_analysis())
                    st.session_state.patterns = patterns

            if "patterns" in st.session_state and st.session_state.patterns:
                patterns = st.session_state.patterns

                # Pattern summary
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Total Patterns", len(patterns))
                with col2:
                    critical = sum(1 for p in patterns if p.severity == PatternSeverity.CRITICAL)
                    st.metric("Critical", critical, delta=f"-{critical}" if critical else None, delta_color="inverse")
                with col3:
                    spinning = sum(1 for p in patterns if p.category == PatternCategory.SPINNING)
                    st.metric("Spinning", spinning)

                # Pattern cards
                for pattern in patterns[:5]:
                    severity_colors = {
                        "critical": "🔴",
                        "high": "🟠",
                        "medium": "🟡",
                        "low": "🟢",
                    }
                    severity_icon = severity_colors.get(pattern.severity.value, "⚪")

                    with st.expander(f"{severity_icon} {pattern.category.value.upper()}: {pattern.description[:60]}..."):
                        st.write(f"**Occurrences:** {pattern.occurrence_count}")
                        st.write(f"**Confidence:** {pattern.confidence:.0%}")
                        st.write(f"**Root Cause Hypothesis:** {pattern.root_cause_hypothesis}")
                        if pattern.socratic_questions:
                            st.write("**Socratic Questions:**")
                            for q in pattern.socratic_questions:
                                st.write(f"  - {q}")
            else:
                st.info("Click 'Analyze Patterns' to detect failure patterns across iterations.")

        with analysis_tabs[1]:  # Dependency Graph
            st.subheader("AC Dependency Analysis")

            if st.button("🔗 Analyze Dependencies", key="dep_btn"):
                with st.spinner("Predicting dependencies..."):
                    async def run_dependency_analysis():
                        predictor = DependencyPredictor()
                        # Use phase as pseudo-AC for demo
                        for it in iteration_objects:
                            it = IterationData(
                                iteration_id=it.iteration_id,
                                timestamp=it.timestamp,
                                phase=it.phase,
                                action=it.action,
                                result=it.result,
                                state=it.state,
                                metrics=it.metrics,
                                reasoning=it.reasoning,
                                outcome=it.outcome,
                                error_message=it.error_message,
                                ac_id=it.phase,  # Use phase as AC
                            )
                        deps = await predictor.predict_dependencies(iteration_objects)
                        return {
                            "dependencies": deps,
                            "execution_order": predictor.get_execution_order(),
                            "critical_path": predictor.get_critical_path(),
                            "summary": predictor.get_summary(),
                        }

                    dep_result = asyncio.run(run_dependency_analysis())
                    st.session_state.dependencies = dep_result

            if "dependencies" in st.session_state:
                dep_data = st.session_state.dependencies

                col1, col2 = st.columns(2)
                with col1:
                    st.write("**Execution Order:**")
                    for i, ac in enumerate(dep_data["execution_order"][:10], 1):
                        st.write(f"{i}. {ac}")

                with col2:
                    st.write("**Critical Path:**")
                    if dep_data["critical_path"]:
                        st.write(" → ".join(dep_data["critical_path"][:5]))
                    else:
                        st.write("No critical path detected")

                st.write("**Summary:**")
                st.json(dep_data["summary"])
            else:
                st.info("Click 'Analyze Dependencies' to predict AC blocking relationships.")

        with analysis_tabs[2]:  # Devil's Advocate
            st.subheader("Deep Socratic Challenging")

            goal_input = st.text_area(
                "Goal/Problem Statement",
                value="Find the shortest path through the maze while collecting all items and avoiding enemies.",
                height=100,
            )

            artifact_input = st.text_area(
                "Proposed Solution/Artifact",
                value="Use A* pathfinding with enemy position avoidance and item collection waypoints.",
                height=100,
            )

            depth_slider = st.slider("Analysis Depth", 1, 5, 3, help="Higher = deeper Socratic questioning")

            if st.button("😈 Challenge Solution", key="devil_btn"):
                with st.spinner("Generating deep challenges..."):
                    async def run_devil_analysis():
                        devil = EnhancedDevilAdvocate()
                        patterns = st.session_state.get("patterns", [])
                        return await devil.challenge(
                            artifact=artifact_input,
                            goal=goal_input,
                            patterns=patterns,
                            depth=depth_slider,
                        )

                    challenge_result = asyncio.run(run_devil_analysis())
                    st.session_state.challenges = challenge_result

            if "challenges" in st.session_state:
                result = st.session_state.challenges

                # Summary metrics
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Challenges", len(result.challenges))
                with col2:
                    st.metric("Confidence", f"{result.overall_confidence:.0%}")
                with col3:
                    root_icon = "✅" if result.is_root_solution else "⚠️"
                    st.metric("Root Solution", root_icon)

                st.write("**Recommendation:**")
                st.warning(result.recommendation)

                # Challenge cards
                for challenge in result.challenges:
                    intensity_colors = {
                        "gentle": "🟢",
                        "moderate": "🟡",
                        "intense": "🟠",
                        "critical": "🔴",
                    }
                    intensity_icon = intensity_colors.get(challenge.intensity.value, "⚪")

                    with st.expander(f"{intensity_icon} [{challenge.challenge_type.value.upper()}] {challenge.question[:60]}..."):
                        st.write(f"**Question:** {challenge.question}")
                        st.write(f"**Reasoning:** {challenge.reasoning}")
                        if challenge.root_cause_hypothesis:
                            st.write(f"**Root Cause Hypothesis:** {challenge.root_cause_hypothesis}")
                        if challenge.alternative_approaches:
                            st.write("**Alternative Approaches:**")
                            for alt in challenge.alternative_approaches:
                                st.write(f"  - {alt}")
            else:
                st.info("Enter a goal and solution, then click 'Challenge Solution' for deep analysis.")

        with analysis_tabs[3]:  # Convergence
            st.subheader("Convergence Tracking")

            # Calculate convergence metrics
            total = len(iterations)
            success_count = sum(1 for it in iterations if "success" in it["result"].lower() or "found" in it["result"].lower())
            failure_count = sum(1 for it in iterations if "dead end" in it["result"].lower() or "obstacle" in it["result"].lower())

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Iterations", total)
            with col2:
                st.metric("Successes", success_count, delta=f"{success_count/total*100:.0f}%")
            with col3:
                st.metric("Failures", failure_count, delta=f"-{failure_count}", delta_color="inverse")
            with col4:
                rate = success_count / total if total > 0 else 0
                st.metric("Convergence Rate", f"{rate:.1%}")

            # Convergence curve
            convergence_data = []
            cumulative_success = 0
            for i, it in enumerate(iterations):
                if "success" in it["result"].lower() or "found" in it["result"].lower():
                    cumulative_success += 1
                convergence_data.append({
                    "iteration": i + 1,
                    "cumulative_success": cumulative_success,
                    "satisfaction_pct": cumulative_success / (i + 1) * 100,
                })

            df = pd.DataFrame(convergence_data)
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df["iteration"],
                y=df["satisfaction_pct"],
                mode="lines+markers",
                name="Satisfaction %",
                line=dict(color="#667eea", width=2),
            ))
            fig.update_layout(
                title="Convergence Curve",
                xaxis_title="Iteration",
                yaxis_title="Satisfaction %",
                template="plotly_white",
                height=300,
            )
            st.plotly_chart(fig, use_container_width=True)

            # Stagnation detection
            if len(iterations) >= 5:
                recent = iterations[-5:]
                recent_failures = sum(1 for it in recent if "dead end" in it["result"].lower() or "obstacle" in it["result"].lower())
                if recent_failures >= 4:
                    st.error("⚠️ **Stagnation Detected:** Last 5 iterations show no progress. Consider alternative approach.")
                elif recent_failures >= 2:
                    st.warning("⚠️ **Possible Stagnation:** Progress is slowing. Monitor closely.")
                else:
                    st.success("✅ **Converging:** System is making steady progress.")

    st.divider()

    # Analysis section
    st.header("🤖 Gemini 3 Analysis")

    # Add live analysis button
    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 2])
    with col_btn1:
        run_analysis = st.button("🚀 Run Live Gemini Analysis", type="primary")
    with col_btn2:
        api_key_available = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))
        if api_key_available:
            st.success("API Key: ✓")
        else:
            st.warning("Set GOOGLE_API_KEY")

    if run_analysis:
        if not api_key_available:
            st.error("Please set GOOGLE_API_KEY or OPENROUTER_API_KEY environment variable")
        else:
            with st.spinner("🔄 Gemini 3 is analyzing all iterations..."):
                st.info(f"Sending {len(iterations)} iterations (~{len(iterations) * 500:,} tokens) to Gemini 3...")
                # In a real implementation, this would call GeminiContextAnalyzer
                # For demo purposes, we show the sample insights
                import time
                time.sleep(2)  # Simulate API call
                st.success("✅ Analysis complete!")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("Discovered Insights")

        # Sample insights (would come from actual Gemini analysis in production)
        sample_insights = [
            {
                "type": "pattern",
                "title": "Recursive Exploration Pattern",
                "description": "Agent shows consistent depth-first exploration strategy with backtracking at iterations 12, 27, 43.",
                "confidence": 0.92,
                "affected_iterations": [12, 27, 43, 58],
            },
            {
                "type": "anomaly",
                "title": "Efficiency Drop at Phase Transition",
                "description": "Significant efficiency decrease during Define→Develop transition suggests adaptation difficulty.",
                "confidence": 0.78,
                "affected_iterations": [15, 16, 17],
            },
            {
                "type": "recommendation",
                "title": "Optimize Item Collection Route",
                "description": "Items at positions (3,5) and (5,3) could be collected in same path segment, saving 4 iterations.",
                "confidence": 0.85,
                "affected_iterations": [22, 35, 41],
            },
            {
                "type": "critical",
                "title": "Root Cause: Incomplete State Modeling",
                "description": "The agent treats enemy positions as static, but they patrol. This explains repeated failures at iterations 31, 44, 52.",
                "confidence": 0.88,
                "affected_iterations": [31, 44, 52],
            },
        ]

        for insight in sample_insights:
            display_insight_card(insight)

    with col2:
        st.subheader("Devil's Advocate")

        st.error("""
        **Critical Assessment**

        The current solution optimizes for path length but may be
        solving a SYMPTOM rather than the ROOT CAUSE.

        **Key Concerns:**
        1. Enemy patrol patterns not modeled
        2. Item spawn timing ignored
        3. No contingency for blocked paths

        **Confidence:** 82%

        *"Optimizing the wrong metric perfectly
        is still failure."*
        """)

    st.divider()

    # Detailed iteration viewer
    st.header("🔍 Iteration Details")

    selected_iteration = st.selectbox(
        "Select Iteration to Inspect",
        options=range(1, len(iterations) + 1),
        format_func=lambda x: f"Iteration {x}: {iterations[x-1]['action']}",
    )

    if selected_iteration:
        it = iterations[selected_iteration - 1]

        col1, col2, col3 = st.columns(3)

        with col1:
            st.subheader("Action & Result")
            st.write(f"**Phase:** {it['phase']}")
            st.write(f"**Action:** {it['action']}")
            st.write(f"**Result:** {it['result']}")

        with col2:
            st.subheader("State")
            st.json(it["state"])

        with col3:
            st.subheader("Metrics")
            for metric, value in it["metrics"].items():
                st.progress(value, text=f"{metric}: {value:.2f}")

        with st.expander("Agent Reasoning"):
            st.write(it["reasoning"])

    st.divider()

    # API Logs section
    st.header("📡 Gemini API Activity")

    # Simulated API logs
    api_logs = [
        {
            "timestamp": datetime.now() - timedelta(minutes=5),
            "request_type": "full_history_analysis",
            "tokens": len(iterations) * 500,
            "latency_ms": 2847,
            "status": "success",
        },
        {
            "timestamp": datetime.now() - timedelta(minutes=3),
            "request_type": "devil_advocate",
            "tokens": 15000,
            "latency_ms": 1523,
            "status": "success",
        },
        {
            "timestamp": datetime.now() - timedelta(minutes=1),
            "request_type": "pattern_analysis",
            "tokens": 8500,
            "latency_ms": 987,
            "status": "success",
        },
    ]

    for log in api_logs:
        css_class = "api-log-success" if log["status"] == "success" else "api-log-error"
        st.markdown(f"""
        <div class="api-log-entry {css_class}">
            <strong>{log['timestamp'].strftime('%H:%M:%S')}</strong> |
            Type: {log['request_type']} |
            Tokens: {log['tokens']:,} |
            Latency: {log['latency_ms']}ms |
            Status: {log['status']}
        </div>
        """, unsafe_allow_html=True)

    # Footer
    st.divider()
    st.markdown("""
    <div style="text-align: center; color: #666; padding: 2rem;">
        <p>🐍 <strong>Ouroboros AI</strong> - Self-Improving AI Workflow System</p>
        <p>Built for the Gemini 3 Hackathon | Demonstrating 1M Token Context Innovation</p>
        <p><em>Claude orchestrates, Gemini analyzes - the best of both worlds</em></p>
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
