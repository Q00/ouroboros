"""Unit tests for TUI widgets."""

import pytest

from ouroboros.tui.widgets.ac_progress import ACProgressItem, ACProgressWidget
from ouroboros.tui.widgets.ac_tree import ACTreeWidget
from ouroboros.tui.widgets.cost_tracker import CostTrackerWidget
from ouroboros.tui.widgets.drift_meter import DriftBar, DriftMeterWidget
from ouroboros.tui.widgets.phase_progress import PhaseIndicator, PhaseProgressWidget


class TestPhaseIndicator:
    """Tests for PhaseIndicator widget."""

    def test_create_phase_indicator(self) -> None:
        """Test creating a phase indicator."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
            is_active=False,
            is_completed=False,
        )

        assert indicator.phase_name == "discover"
        assert indicator.phase_type == "diverge"
        assert indicator.has_class("diverge")

    def test_active_indicator(self) -> None:
        """Test active phase indicator."""
        indicator = PhaseIndicator(
            phase_name="define",
            phase_label="Define",
            phase_type="converge",
            is_active=True,
        )

        assert indicator.has_class("active")

    def test_completed_indicator(self) -> None:
        """Test completed phase indicator."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
            is_completed=True,
        )

        assert indicator.has_class("completed")

    def test_set_active(self) -> None:
        """Test setting active state."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
        )

        indicator.set_active(True)
        assert indicator.has_class("active")

        indicator.set_active(False)
        assert not indicator.has_class("active")

    def test_set_completed(self) -> None:
        """Test setting completed state."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
        )

        indicator.set_completed(True)
        assert indicator.has_class("completed")

        indicator.set_completed(False)
        assert not indicator.has_class("completed")


class TestPhaseProgressWidget:
    """Tests for PhaseProgressWidget."""

    def test_create_widget(self) -> None:
        """Test creating phase progress widget."""
        widget = PhaseProgressWidget(current_phase="discover", iteration=1)

        assert widget.current_phase == "discover"
        assert widget.iteration == 1

    def test_update_phase(self) -> None:
        """Test updating current phase."""
        widget = PhaseProgressWidget()

        widget.update_phase("define", iteration=2)

        assert widget.current_phase == "define"
        assert widget.iteration == 2

    def test_is_phase_completed(self) -> None:
        """Test phase completion check."""
        widget = PhaseProgressWidget(current_phase="design")

        # Discover and Define should be completed
        assert widget._is_phase_completed("discover") is True
        assert widget._is_phase_completed("define") is True
        # Design and Deliver should not be completed
        assert widget._is_phase_completed("design") is False
        assert widget._is_phase_completed("deliver") is False

    def test_is_phase_completed_no_current(self) -> None:
        """Test phase completion when no current phase."""
        widget = PhaseProgressWidget(current_phase="")

        assert widget._is_phase_completed("discover") is False


class TestDriftBar:
    """Tests for DriftBar widget."""

    def test_create_drift_bar(self) -> None:
        """Test creating drift bar."""
        bar = DriftBar(label="Goal", value=0.15)

        assert bar._label == "Goal"
        assert bar.value == 0.15

    def test_drift_bar_threshold(self) -> None:
        """Test drift bar with custom threshold."""
        bar = DriftBar(label="Test", value=0.4, threshold=0.3)

        # Should have warning class since 0.4 > 0.3
        # Note: Classes are applied after mount, so we test the value
        assert bar.value == 0.4
        assert bar._threshold == 0.3


class TestDriftMeterWidget:
    """Tests for DriftMeterWidget."""

    def test_create_widget(self) -> None:
        """Test creating drift meter widget."""
        widget = DriftMeterWidget(
            goal_drift=0.15,
            constraint_drift=0.1,
            ontology_drift=0.05,
        )

        assert widget.goal_drift == 0.15
        assert widget.constraint_drift == 0.1
        assert widget.ontology_drift == 0.05

    def test_combined_drift_calculation(self) -> None:
        """Test combined drift calculation matches PRD formula."""
        widget = DriftMeterWidget(
            goal_drift=0.2,
            constraint_drift=0.1,
            ontology_drift=0.05,
        )

        # Formula: (goal * 0.5) + (constraint * 0.3) + (ontology * 0.2)
        expected = (0.2 * 0.5) + (0.1 * 0.3) + (0.05 * 0.2)
        assert abs(widget.combined_drift - expected) < 0.001

    def test_is_acceptable_under_threshold(self) -> None:
        """Test is_acceptable when under threshold."""
        widget = DriftMeterWidget(
            goal_drift=0.1,
            constraint_drift=0.1,
            ontology_drift=0.1,
        )

        # Combined = 0.05 + 0.03 + 0.02 = 0.10, under 0.3
        assert widget.is_acceptable is True

    def test_is_acceptable_over_threshold(self) -> None:
        """Test is_acceptable when over threshold."""
        widget = DriftMeterWidget(
            goal_drift=0.5,
            constraint_drift=0.5,
            ontology_drift=0.5,
        )

        # Combined = 0.25 + 0.15 + 0.10 = 0.50, over 0.3
        assert widget.is_acceptable is False

    def test_update_drift(self) -> None:
        """Test updating drift values."""
        widget = DriftMeterWidget()

        widget.update_drift(
            goal_drift=0.2,
            constraint_drift=0.15,
            ontology_drift=0.1,
        )

        assert widget.goal_drift == 0.2
        assert widget.constraint_drift == 0.15
        assert widget.ontology_drift == 0.1

    def test_update_drift_partial(self) -> None:
        """Test partial drift update."""
        widget = DriftMeterWidget(
            goal_drift=0.1,
            constraint_drift=0.1,
            ontology_drift=0.1,
        )

        widget.update_drift(goal_drift=0.3)

        assert widget.goal_drift == 0.3
        assert widget.constraint_drift == 0.1  # Unchanged
        assert widget.ontology_drift == 0.1  # Unchanged


class TestACTreeWidget:
    """Tests for ACTreeWidget."""

    def test_create_widget_empty(self) -> None:
        """Test creating empty AC tree widget."""
        widget = ACTreeWidget()

        assert widget.tree_data == {}
        assert widget.current_ac_id == ""

    def test_create_widget_with_data(self) -> None:
        """Test creating widget with tree data."""
        tree_data = {
            "root_id": "ac_123",
            "nodes": {
                "ac_123": {
                    "id": "ac_123",
                    "content": "Root AC",
                    "depth": 0,
                    "status": "pending",
                    "is_atomic": False,
                    "children_ids": [],
                },
            },
        }

        widget = ACTreeWidget(tree_data=tree_data, current_ac_id="ac_123")

        assert widget.tree_data == tree_data
        assert widget.current_ac_id == "ac_123"

    def test_update_tree(self) -> None:
        """Test updating tree data."""
        widget = ACTreeWidget()
        tree_data = {"root_id": "ac_456", "nodes": {}}

        widget.update_tree(tree_data, current_ac_id="ac_456")

        assert widget.tree_data == tree_data
        assert widget.current_ac_id == "ac_456"

    def test_update_node_status(self) -> None:
        """Test updating a node's status."""
        tree_data = {
            "root_id": "ac_123",
            "nodes": {
                "ac_123": {
                    "id": "ac_123",
                    "content": "Test AC",
                    "depth": 0,
                    "status": "pending",
                    "is_atomic": False,
                    "children_ids": [],
                },
            },
        }
        widget = ACTreeWidget(tree_data=tree_data)

        widget.update_node_status("ac_123", "completed")

        assert widget.tree_data["nodes"]["ac_123"]["status"] == "completed"


class TestCostTrackerWidget:
    """Tests for CostTrackerWidget."""

    def test_create_widget(self) -> None:
        """Test creating cost tracker widget."""
        widget = CostTrackerWidget(
            total_tokens=5000,
            total_cost_usd=0.025,
            tokens_this_phase=1000,
            model_name="gpt-4",
        )

        assert widget.total_tokens == 5000
        assert widget.total_cost_usd == 0.025
        assert widget.tokens_this_phase == 1000
        assert widget.model_name == "gpt-4"

    def test_format_tokens_small(self) -> None:
        """Test token formatting for small values."""
        widget = CostTrackerWidget()

        assert widget._format_tokens(500) == "500"

    def test_format_tokens_thousands(self) -> None:
        """Test token formatting for thousands."""
        widget = CostTrackerWidget()

        assert widget._format_tokens(5000) == "5.0K"
        assert widget._format_tokens(12500) == "12.5K"

    def test_format_tokens_millions(self) -> None:
        """Test token formatting for millions."""
        widget = CostTrackerWidget()

        assert widget._format_tokens(1500000) == "1.5M"

    def test_format_cost_small(self) -> None:
        """Test cost formatting for small values."""
        widget = CostTrackerWidget()

        assert widget._format_cost(0.005) == "$0.0050"

    def test_format_cost_medium(self) -> None:
        """Test cost formatting for medium values."""
        widget = CostTrackerWidget()

        assert widget._format_cost(0.5) == "$0.500"

    def test_format_cost_large(self) -> None:
        """Test cost formatting for large values."""
        widget = CostTrackerWidget()

        assert widget._format_cost(5.25) == "$5.25"

    def test_truncate_model(self) -> None:
        """Test model name truncation."""
        widget = CostTrackerWidget()

        assert widget._truncate_model("gpt-4") == "gpt-4"
        assert widget._truncate_model("openrouter/google/gemini-2.0-flash-001") == "openrouter/g..."

    def test_update_cost(self) -> None:
        """Test updating cost values."""
        widget = CostTrackerWidget()

        widget.update_cost(
            total_tokens=10000,
            total_cost_usd=0.05,
            tokens_this_phase=2000,
        )

        assert widget.total_tokens == 10000
        assert widget.total_cost_usd == 0.05
        assert widget.tokens_this_phase == 2000

    def test_add_tokens(self) -> None:
        """Test adding tokens to totals."""
        widget = CostTrackerWidget(
            total_tokens=5000,
            total_cost_usd=0.025,
        )

        widget.add_tokens(1000, cost=0.005)

        assert widget.total_tokens == 6000
        assert widget.total_cost_usd == pytest.approx(0.03)
        assert widget.tokens_this_phase == 1000

    def test_reset_phase_tokens(self) -> None:
        """Test resetting phase token counter."""
        widget = CostTrackerWidget(tokens_this_phase=1000)

        widget.reset_phase_tokens()

        assert widget.tokens_this_phase == 0

    def test_get_cost_class(self) -> None:
        """Test cost class determination."""
        widget = CostTrackerWidget()

        widget.total_cost_usd = 0.5
        assert widget._get_cost_class() == ""

        widget.total_cost_usd = 1.5
        assert widget._get_cost_class() == "high"

        widget.total_cost_usd = 15.0
        assert widget._get_cost_class() == "very-high"


class TestACProgressItem:
    """Tests for ACProgressItem dataclass."""

    def test_create_item(self) -> None:
        """Test creating an AC progress item."""
        item = ACProgressItem(
            index=1,
            content="Create a hello.py file",
            status="pending",
        )

        assert item.index == 1
        assert item.content == "Create a hello.py file"
        assert item.status == "pending"
        assert item.elapsed_display == ""
        assert item.is_current is False

    def test_create_item_with_elapsed(self) -> None:
        """Test creating item with elapsed time."""
        item = ACProgressItem(
            index=2,
            content="Run tests",
            status="in_progress",
            elapsed_display="45s",
            is_current=True,
        )

        assert item.index == 2
        assert item.status == "in_progress"
        assert item.elapsed_display == "45s"
        assert item.is_current is True


class TestACProgressWidget:
    """Tests for ACProgressWidget."""

    def test_create_widget_empty(self) -> None:
        """Test creating an empty progress widget."""
        widget = ACProgressWidget()

        assert widget.acceptance_criteria == []
        assert widget.completed_count == 0
        assert widget.total_count == 0

    def test_create_widget_with_criteria(self) -> None:
        """Test creating widget with acceptance criteria."""
        items = [
            ACProgressItem(index=1, content="AC 1", status="completed"),
            ACProgressItem(index=2, content="AC 2", status="in_progress"),
            ACProgressItem(index=3, content="AC 3", status="pending"),
        ]

        widget = ACProgressWidget(
            acceptance_criteria=items,
            completed_count=1,
            total_count=3,
        )

        assert len(widget.acceptance_criteria) == 3
        assert widget.completed_count == 1
        assert widget.total_count == 3

    def test_update_progress(self) -> None:
        """Test updating progress."""
        widget = ACProgressWidget()

        items = [
            ACProgressItem(index=1, content="AC 1", status="completed"),
        ]

        widget.update_progress(
            acceptance_criteria=items,
            completed_count=1,
            total_count=2,
            estimated_remaining="~5m remaining",
        )

        assert len(widget.acceptance_criteria) == 1
        assert widget.completed_count == 1
        assert widget.total_count == 2
        assert widget.estimated_remaining == "~5m remaining"

    def test_update_progress_partial(self) -> None:
        """Test partial progress update."""
        widget = ACProgressWidget(
            completed_count=0,
            total_count=3,
        )

        widget.update_progress(completed_count=1)

        assert widget.completed_count == 1
        assert widget.total_count == 3  # Unchanged
