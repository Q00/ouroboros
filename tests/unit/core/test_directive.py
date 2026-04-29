"""Unit tests for ouroboros.core.directive module."""

from ouroboros.core.directive import Directive


class TestDirectiveValues:
    """Directive member string values are stable and unique."""

    def test_directive_values_are_lowercase_names(self) -> None:
        """Every directive's string value matches its lowercased member name."""
        for member in Directive:
            assert member.value == member.name.lower()

    def test_directive_values_are_unique(self) -> None:
        """No two directives share a string value."""
        values = [member.value for member in Directive]

        assert len(values) == len(set(values))

    def test_directive_is_str_subclass(self) -> None:
        """Directive members are usable as strings (StrEnum contract)."""
        assert Directive.CONTINUE == "continue"
        assert Directive.CONVERGE == "converge"


class TestDirectiveTerminality:
    """Terminality is a closed, documented property of the vocabulary."""

    def test_cancel_is_terminal(self) -> None:
        """CANCEL ends the execution."""
        assert Directive.CANCEL.is_terminal is True

    def test_converge_is_terminal(self) -> None:
        """CONVERGE ends the execution."""
        assert Directive.CONVERGE.is_terminal is True

    def test_non_terminal_directives_are_not_terminal(self) -> None:
        """Every directive other than CANCEL and CONVERGE continues the run."""
        terminals = {Directive.CANCEL, Directive.CONVERGE}
        for member in Directive:
            if member in terminals:
                continue
            assert member.is_terminal is False, f"{member} should not be terminal"

    def test_exactly_two_terminal_directives(self) -> None:
        """The terminal set is intentionally small; guard against accidental growth."""
        terminals = [member for member in Directive if member.is_terminal]

        assert len(terminals) == 2


class TestDirectiveMembership:
    """Vocabulary membership is an API commitment — renaming or removing
    members is a breaking change, so guard it with explicit assertions."""

    def test_required_members_present(self) -> None:
        """The initial vocabulary set must be preserved."""
        required = {
            "CONTINUE",
            "EVALUATE",
            "EVOLVE",
            "UNSTUCK",
            "RETRY",
            "COMPACT",
            "WAIT",
            "CANCEL",
            "CONVERGE",
        }
        names = {member.name for member in Directive}

        assert required.issubset(names)

    def test_lookup_by_value_round_trips(self) -> None:
        """Directive(value) returns the same member it was created from."""
        for member in Directive:
            assert Directive(member.value) is member


class TestDirectiveCoreReExport:
    """Directive is exposed at the ouroboros.core package boundary."""

    def test_directive_importable_from_core(self) -> None:
        """Directive is re-exported via the lazy loader in ouroboros.core."""
        from ouroboros.core import Directive as CoreDirective

        assert CoreDirective is Directive
