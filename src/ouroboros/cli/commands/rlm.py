"""Recursive Language Model command for Ouroboros."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success
from ouroboros.rlm import (
    MAX_RLM_AC_TREE_DEPTH,
    MAX_RLM_AMBIGUITY_THRESHOLD,
    RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID,
    RLM_MVP_SRC_DOGFOOD_TARGET_CORPUS,
    RLMRunConfig,
    RLMRunResult,
    RLMSharedTruncationBenchmarkConfig,
    RLMSharedTruncationBenchmarkResult,
    RLMTraceStore,
    RLMVanillaTruncationBaselineConfig,
    load_recursive_fixture,
    load_truncation_fixture,
    run_rlm_benchmark,
    run_rlm_loop,
    run_shared_truncation_benchmark,
    run_vanilla_truncation_baseline,
)


async def _run_with_default_trace_store(
    config: RLMRunConfig,
    *,
    benchmark_id: str | None = None,
) -> RLMRunResult:
    """Run RLM with the default EventStore-backed trace sink for command invocations."""

    async def run_selected(resolved_config: RLMRunConfig) -> RLMRunResult:
        if benchmark_id is None:
            return await run_rlm_loop(resolved_config)
        return await run_rlm_benchmark(resolved_config, benchmark_id=benchmark_id)

    if config.dry_run:
        return await run_selected(config)

    from ouroboros.persistence.event_store import EventStore

    event_store = EventStore()
    await event_store.initialize()
    try:
        return await run_selected(
            replace(config, trace_store=RLMTraceStore(event_store)),
        )
    finally:
        await event_store.close()


async def _run_shared_truncation_with_default_trace_store(
    config: RLMSharedTruncationBenchmarkConfig,
) -> RLMSharedTruncationBenchmarkResult:
    """Run the shared truncation benchmark with the default RLM trace sink."""
    from ouroboros.persistence.event_store import EventStore

    event_store = EventStore()
    await event_store.initialize()
    try:
        return await run_shared_truncation_benchmark(
            replace(config, trace_store=RLMTraceStore(event_store)),
        )
    finally:
        await event_store.close()


def _default_truncation_fixture_path(cwd: Path) -> Path:
    """Return the repo-local truncation fixture path used by the baseline MVP."""
    return cwd / "tests" / "fixtures" / "rlm" / "long_context_truncation.json"


def command(
    target: Annotated[
        str,
        typer.Argument(
            help=("RLM target prompt or path. Defaults to dogfooding the local src tree."),
        ),
    ] = RLM_MVP_SRC_DOGFOOD_TARGET_CORPUS.root,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help="Working directory for the isolated RLM invocation.",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            min=0,
            max=MAX_RLM_AC_TREE_DEPTH,
            help="Maximum recursive AC tree depth for the RLM MVP.",
        ),
    ] = MAX_RLM_AC_TREE_DEPTH,
    ambiguity_threshold: Annotated[
        float,
        typer.Option(
            "--ambiguity-threshold",
            min=0.0,
            max=MAX_RLM_AMBIGUITY_THRESHOLD,
            help="Maximum allowed ambiguity score before RLM execution.",
        ),
    ] = MAX_RLM_AMBIGUITY_THRESHOLD,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate the RLM path without side effects."),
    ] = False,
    benchmark: Annotated[
        bool,
        typer.Option(
            "--benchmark",
            help="Run the built-in RLM dogfood benchmark through the recursive loop.",
        ),
    ] = False,
    benchmark_id: Annotated[
        str | None,
        typer.Option(
            "--benchmark-id",
            help="Built-in RLM benchmark ID to run through the recursive loop.",
        ),
    ] = None,
    vanilla_baseline: Annotated[
        bool,
        typer.Option(
            "--vanilla-baseline",
            help="Run the long-context truncation fixture through one Hermes baseline call.",
        ),
    ] = False,
    truncation_benchmark: Annotated[
        bool,
        typer.Option(
            "--truncation-benchmark",
            help=(
                "Run vanilla Hermes and recursive RLM against the same truncation "
                "fixture and persist a side-by-side artifact."
            ),
        ),
    ] = False,
    truncation_fixture: Annotated[
        Path | None,
        typer.Option(
            "--truncation-fixture",
            help=(
                "Fixture JSON for --vanilla-baseline or --truncation-benchmark. "
                "Defaults to tests/fixtures/rlm."
            ),
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    recursive_fixture: Annotated[
        Path | None,
        typer.Option(
            "--recursive-fixture",
            help=(
                "Fixture JSON for the recursive RLM path. The fixture supplies "
                "target, prompt, initial state, loop limits, and expected outputs."
            ),
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    baseline_output: Annotated[
        Path | None,
        typer.Option(
            "--baseline-output",
            help=(
                "Persisted JSON output path for --vanilla-baseline, or the vanilla "
                "component of --truncation-benchmark."
            ),
            file_okay=True,
            dir_okay=False,
        ),
    ] = None,
    truncation_output: Annotated[
        Path | None,
        typer.Option(
            "--truncation-output",
            help="Persisted side-by-side JSON output path for --truncation-benchmark.",
            file_okay=True,
            dir_okay=False,
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option("--debug", "-d", help="Show RLM bootstrap details."),
    ] = False,
) -> None:
    """Run the isolated Dual-layer Recursive Language Model MVP path.

    This command is the terminal counterpart to the ``ooo rlm`` skill. It owns a
    separate RLM path and does not dispatch through ``ouroboros run`` or
    ``ouroboros evolve``.
    """
    resolved_cwd = cwd or Path.cwd()
    if truncation_benchmark and (
        vanilla_baseline or benchmark or benchmark_id is not None or recursive_fixture is not None
    ):
        print_error(
            "Use --truncation-benchmark by itself, not with --vanilla-baseline, "
            "--recursive-fixture, or --benchmark/--benchmark-id."
        )
        raise typer.Exit(1)
    if vanilla_baseline and (benchmark or benchmark_id is not None):
        print_error("Use either --vanilla-baseline or --benchmark/--benchmark-id, not both.")
        raise typer.Exit(1)
    if vanilla_baseline and recursive_fixture is not None:
        print_error("Use either --vanilla-baseline or --recursive-fixture, not both.")
        raise typer.Exit(1)
    if recursive_fixture is not None and (benchmark or benchmark_id is not None):
        print_error("Use either --recursive-fixture or --benchmark/--benchmark-id, not both.")
        raise typer.Exit(1)
    if truncation_benchmark:
        fixture_path = truncation_fixture or _default_truncation_fixture_path(resolved_cwd)
        try:
            fixture = load_truncation_fixture(fixture_path)
            shared_result = asyncio.run(
                _run_shared_truncation_with_default_trace_store(
                    RLMSharedTruncationBenchmarkConfig(
                        fixture=fixture,
                        cwd=resolved_cwd,
                        result_path=truncation_output,
                        baseline_result_path=baseline_output,
                    )
                )
            )
        except ValueError as exc:
            print_error(str(exc))
            raise typer.Exit(1) from exc

        if shared_result.success:
            print_success(
                "Shared truncation benchmark completed; vanilla Hermes and recursive RLM "
                "outputs were recorded."
            )
        else:
            print_error("Shared truncation benchmark failed.")
        console.print(f"[dim]fixture:[/] {fixture_path}")
        console.print(f"[dim]target:[/] {shared_result.target_path} (truncation fixture)")
        if shared_result.result_path is not None:
            console.print(f"[dim]truncation_result:[/] {shared_result.result_path}")
        if shared_result.vanilla_result.result_path is not None:
            console.print(f"[dim]vanilla_result:[/] {shared_result.vanilla_result.result_path}")
        console.print(
            "[dim]hermes_subcalls:[/] "
            f"vanilla={shared_result.vanilla_result.hermes_subcall_count}, "
            f"rlm={shared_result.rlm_result.hermes_subcall_count}"
        )
        console.print(
            "[dim]chunks:[/] "
            f"selected={len(shared_result.selected_chunk_ids)}, "
            f"omitted={len(shared_result.omitted_chunk_ids)}"
        )
        if shared_result.quality_comparison is not None:
            quality = shared_result.quality_comparison
            console.print(
                "[dim]quality:[/] "
                f"vanilla={quality.vanilla_quality.score:.2f}, "
                f"rlm={quality.rlm_quality.score:.2f}, "
                f"delta={quality.score_delta:+.2f}, "
                f"rlm_outperforms_vanilla={quality.rlm_outperforms_vanilla}"
            )
        if debug:
            console.print(f"[dim]benchmark_id:[/] {shared_result.benchmark_id}")
            console.print(f"[dim]fixture_id:[/] {shared_result.fixture_id}")
        if not shared_result.success:
            raise typer.Exit(1)
        return

    if vanilla_baseline:
        fixture_path = truncation_fixture or _default_truncation_fixture_path(resolved_cwd)
        try:
            fixture = load_truncation_fixture(fixture_path)
            baseline_result = asyncio.run(
                run_vanilla_truncation_baseline(
                    RLMVanillaTruncationBaselineConfig(
                        fixture=fixture,
                        cwd=resolved_cwd,
                        result_path=baseline_output,
                    )
                )
            )
        except ValueError as exc:
            print_error(str(exc))
            raise typer.Exit(1) from exc

        if baseline_result.success:
            print_success(
                "Vanilla Hermes baseline completed with one sub-call; "
                "recursive RLM loop was not invoked."
            )
        else:
            print_error("Vanilla Hermes baseline failed.")
        console.print(f"[dim]fixture:[/] {fixture_path}")
        console.print(f"[dim]target:[/] {baseline_result.target_path} (truncation fixture)")
        if baseline_result.result_path is not None:
            console.print(f"[dim]baseline_result:[/] {baseline_result.result_path}")
        console.print(f"[dim]hermes_subcalls:[/] {baseline_result.hermes_subcall_count}")
        console.print(
            "[dim]chunks:[/] "
            f"selected={len(baseline_result.selected_chunk_ids)}, "
            f"omitted={len(baseline_result.omitted_chunk_ids)}"
        )
        console.print(f"[dim]output_quality_score:[/] {baseline_result.output_quality.score:.2f}")
        if debug:
            console.print(f"[dim]baseline_id:[/] {baseline_result.baseline_id}")
            console.print(f"[dim]call_id:[/] {baseline_result.call_id}")
        if not baseline_result.success:
            raise typer.Exit(1)
        return

    recursive_fixture_spec = None
    if recursive_fixture is not None:
        try:
            recursive_fixture_spec = load_recursive_fixture(recursive_fixture)
            if not dry_run:
                recursive_fixture_spec.write_target(resolved_cwd)
        except ValueError as exc:
            print_error(str(exc))
            raise typer.Exit(1) from exc

    if recursive_fixture_spec is None:
        config = RLMRunConfig(
            target=target,
            cwd=resolved_cwd,
            max_depth=max_depth,
            ambiguity_threshold=ambiguity_threshold,
            dry_run=dry_run,
            debug=debug,
        )
    else:
        config = recursive_fixture_spec.to_run_config(
            cwd=resolved_cwd,
            dry_run=dry_run,
            debug=debug,
        )
    selected_benchmark_id = (
        benchmark_id
        if benchmark_id is not None
        else RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
        if benchmark
        else None
    )

    try:
        result = asyncio.run(
            _run_with_default_trace_store(config, benchmark_id=selected_benchmark_id)
        )
        if recursive_fixture_spec is not None and not config.dry_run:
            recursive_fixture_spec.assert_result_matches(result)
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    if result.status == "ready":
        print_info(result.message)
    else:
        print_success(result.message)

    console.print(f"[dim]target:[/] {result.target} ({result.target_kind})")
    console.print(f"[dim]cwd:[/] {result.cwd}")
    if recursive_fixture_spec is not None:
        console.print(f"[dim]fixture:[/] {recursive_fixture_spec.source_path}")
    if result.benchmark_output is not None:
        benchmark_output = result.benchmark_output
        console.print(
            "[dim]benchmark:[/] "
            f"{benchmark_output.benchmark_id}; "
            f"cited_source_files={benchmark_output.cited_source_file_count}; "
            f"rlm_tree_depth={benchmark_output.generated_rlm_tree_depth}"
        )
        for evidence in benchmark_output.source_evidence[:3]:
            console.print(f"[dim]evidence:[/] {evidence.evidence_id} - {evidence.claim}")
    if debug:
        console.print(
            "[dim]rlm guardrails:[/] "
            f"max_depth={result.max_depth}, "
            f"ambiguity_threshold={result.ambiguity_threshold}"
        )


__all__ = ["command"]
