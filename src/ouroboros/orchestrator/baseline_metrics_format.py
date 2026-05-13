"""Human-readable formatter for fat-harness baseline metric reports.

Companion to :mod:`ouroboros.orchestrator.baseline_metrics`. The
report module owns the data model and gate evaluation; this module
owns presentation. They are kept separate so the JSON contract in
:meth:`FatHarnessMetricsReport.to_dict` stays untouched by any
display logic.

The formatter is intentionally a pure function: it takes a frozen
report and returns a string. No I/O, no LLM, no live execution. It
is safe to call from CLI handlers, CI summaries, or test assertions.

This module addresses a small but practical gap on the path to
closing the ``agentos-substrate-wiring`` milestone — the hard-gate
baseline metrics must be reported in a human-checkable form so a
maintainer can compare the values against the gate criteria without
parsing JSON by hand. The report content itself (sample collection,
fat-harness execution wiring) lives upstream in dedicated PRs; this
PR adds only the rendering surface.
"""

from __future__ import annotations

from collections.abc import Iterable

from ouroboros.orchestrator.baseline_metrics import (
    FatHarnessGateResult,
    FatHarnessGateStatus,
    FatHarnessMetricsReport,
)

_STATUS_GLYPH: dict[FatHarnessGateStatus, str] = {
    FatHarnessGateStatus.PASS: "PASS",
    FatHarnessGateStatus.FAIL: "FAIL",
    FatHarnessGateStatus.CAPTURED: "CAPT",
    FatHarnessGateStatus.NOT_APPLICABLE: " N/A",
}


def render_baseline_report(report: FatHarnessMetricsReport) -> str:
    """Return a multi-line human-readable rendering of ``report``.

    The output is stable and deterministic — repeat calls against the
    same report return byte-identical text — so CI fixtures and
    snapshot tests can rely on it without floating-point fuzzing.

    Format:

    ```
    Fat-harness baseline report — profile=<name> · acs=<n> · K=<max_retries>
    --------------------------------------------------------------
      [PASS] one_shot_pass_rate           : 0.83 (target ≥ ?)
      [PASS] k_recovery_rate              : 0.71 (target ≥ 0.70)
      ...
    ```

    The exact targets shown depend on the gate status field; the
    formatter never invents thresholds.
    """
    header_line = (
        f"Fat-harness baseline report — profile={report.profile} · "
        f"acs={report.total_acs} · K={report.max_retries}"
    )
    rule = "-" * len(header_line)

    lines: list[str] = [header_line, rule]
    lines.extend(_render_gate_lines(report.gates))
    lines.append(rule)
    lines.extend(_render_metric_summary(report))
    return "\n".join(lines)


def _render_gate_lines(gates: Iterable[FatHarnessGateResult]) -> list[str]:
    rendered: list[str] = []
    for gate in gates:
        glyph = _STATUS_GLYPH.get(gate.status, "????")
        value = _format_value(gate.value)
        target_part = _format_target(gate)
        rendered.append(f"  [{glyph}] {gate.name:<35} : {value}{target_part}")
    return rendered


def _render_metric_summary(report: FatHarnessMetricsReport) -> list[str]:
    metrics: tuple[tuple[str, str], ...] = (
        ("one_shot_pass_rate", _format_value(report.one_shot_pass_rate)),
        ("k_recovery_rate", _format_value(report.k_recovery_rate)),
        (
            "fabrication_incidents_per_100_acs",
            _format_value(report.fabrication_incidents_per_100_acs),
        ),
        (
            "semantic_miss_incidents_per_100_acs",
            _format_value(report.semantic_miss_incidents_per_100_acs),
        ),
        ("median_chars_per_ac", _format_value(report.median_chars_per_ac)),
        ("new_domain_loc_delta", str(report.new_domain_loc_delta)),
        ("new_domain_yaml_delta", str(report.new_domain_yaml_delta)),
    )
    return [f"  {label:<39}: {value}" for label, value in metrics]


def _format_value(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    if value != value:  # NaN guard — should not arise, but cheap to keep
        return "nan"
    return f"{value:.4f}"


def _format_target(gate: FatHarnessGateResult) -> str:
    target = getattr(gate, "target", "")
    if not target:
        return ""
    return f" (target {target})"


__all__ = [
    "render_baseline_report",
]
