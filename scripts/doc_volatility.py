#!/usr/bin/env python3
"""
doc_volatility.py — Documentation Volatility Scorer for Ouroboros
==================================================================

Queries ``git log --since=3.months`` to collect recently changed files,
maps them against each document's declared ``code_deps`` in
``docs/doc-topology.yaml``, and computes a numeric volatility score
per document.

A *volatile* document is one whose code dependencies have changed
frequently in the last 3 months — meaning the document is most
likely to be stale and in need of review.

Volatility score definition
----------------------------
For each document *D* with ``code_deps`` list *C*:

  commit_hits(D)   = Σ (number of commits that touched dep p) for p in C
  unique_dep_hits(D) = |{p ∈ C : p was touched at least once}|
  coverage(D)      = unique_dep_hits(D) / max(|C|, 1)
  volatility(D)    = commit_hits(D)

``commit_hits`` is the primary sort key — it directly reflects how
much activity the doc's underlying code has seen.  ``coverage`` is a
secondary signal showing the *breadth* of change (many deps touched
vs. one dep changed many times).

Directory deps (e.g. ``src/ouroboros/cli/commands/``) are expanded:
any changed file whose path starts with that prefix counts as a hit.

Usage
-----
Run from the repo root::

    python scripts/doc_volatility.py [--since PERIOD] [--output PATH] [--top N]

Options
    --since PERIOD   git-log period string  (default: ``3.months``)
    --output PATH    write Markdown report to PATH instead of stdout
    --top N          only show top N documents in the report (default: all)
    --topology PATH  path to doc-topology.yaml  (default: docs/doc-topology.yaml)

Exit codes
    0   success
    1   docs/doc-topology.yaml not found
    2   git executable not found / not in a git repo
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys

try:
    import yaml  # PyYAML
except ImportError:
    yaml = None  # handled below with a friendly message


# ---------------------------------------------------------------------------
# Data classes (stdlib only — no attrs/pydantic)
# ---------------------------------------------------------------------------


class DocEntry:
    """Represents one entry from docs/doc-topology.yaml."""

    def __init__(self, doc_key: str, code_deps: list[str]) -> None:
        self.doc_key = doc_key  # e.g. "docs/cli-reference.md"
        self.code_deps: list[str] = code_deps  # raw dep paths/dirs from YAML

    def __repr__(self) -> str:  # pragma: no cover
        return f"DocEntry({self.doc_key!r}, deps={len(self.code_deps)})"


class VolatilityResult:
    """Volatility score for a single document."""

    def __init__(
        self,
        doc_key: str,
        code_deps: list[str],
        commit_hits: int,
        unique_dep_hits: int,
        touched_deps: list[str],
        commit_detail: dict[str, int],
    ) -> None:
        self.doc_key = doc_key
        self.code_deps = code_deps
        self.commit_hits = commit_hits  # primary score
        self.unique_dep_hits = unique_dep_hits
        self.total_deps = len(code_deps)
        self.touched_deps = touched_deps  # which deps were hit
        self.commit_detail = commit_detail  # dep -> commit count

    @property
    def coverage(self) -> float:
        """Fraction of declared deps that were touched (0.0–1.0)."""
        if not self.code_deps:
            return 0.0
        return self.unique_dep_hits / len(self.code_deps)

    @property
    def volatility_score(self) -> int:
        """Primary numeric score: total (dep, commit) hit count."""
        return self.commit_hits

    def risk_label(self) -> str:
        """Human-readable risk band."""
        if self.commit_hits == 0:
            return "STABLE"
        if self.commit_hits <= 3:
            return "LOW"
        if self.commit_hits <= 10:
            return "MEDIUM"
        if self.commit_hits <= 25:
            return "HIGH"
        return "CRITICAL"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a subprocess and return stdout; raise RuntimeError on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {cmd[0]}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"Command {' '.join(cmd)!r} failed (rc={result.returncode}):\n{result.stderr.strip()}"
        )
    return result.stdout


def collect_changed_files(
    repo_root: Path,
    since: str,
) -> dict[str, int]:
    """
    Return a dict mapping ``repo-root-relative path → commit count``
    for all files changed in any commit since *since*.

    We use ``git log --name-only`` with a sentinel prefix so we can
    parse the output without ambiguity.
    """
    raw = _run(
        ["git", "log", f"--since={since}", "--name-only", "--pretty=format:COMMIT:%H"],
        cwd=repo_root,
    )

    file_commit_count: dict[str, int] = defaultdict(int)
    in_commit = False

    for line in raw.splitlines():
        if line.startswith("COMMIT:"):
            in_commit = True
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if in_commit:
            file_commit_count[stripped] += 1

    return dict(file_commit_count)


def get_repo_root() -> Path:
    """Return the repo root by querying git."""
    raw = _run(["git", "rev-parse", "--show-toplevel"])
    return Path(raw.strip())


# ---------------------------------------------------------------------------
# Topology loading
# ---------------------------------------------------------------------------


def load_topology(topology_path: Path) -> list[DocEntry]:
    """Parse doc-topology.yaml and return a list of DocEntry objects."""
    if yaml is None:
        raise ImportError(
            "PyYAML is required but not installed.\n"
            "Install it with:  pip install pyyaml   or  uv add pyyaml"
        )

    with topology_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    entries: list[DocEntry] = []
    docs_section = data.get("docs", {}) or {}

    for doc_key, meta in docs_section.items():
        if not isinstance(meta, dict):
            continue
        raw_deps = meta.get("code_deps", []) or []
        # Strip inline comments (YAML values sometimes include # ...)
        clean_deps: list[str] = []
        for dep in raw_deps:
            dep_str = str(dep).split("#")[0].strip()
            if dep_str:
                clean_deps.append(dep_str)
        entries.append(DocEntry(doc_key=doc_key, code_deps=clean_deps))

    return entries


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------


def _dep_matches_changed_file(dep: str, changed_file: str) -> bool:
    """
    Return True if *changed_file* (repo-root-relative) is covered by *dep*.

    - Exact match:    dep == changed_file
    - Directory dep:  dep ends with '/' and changed_file starts with dep prefix
    - Directory dep (no trailing slash): dep is a prefix of changed_file followed by '/'
    """
    # Normalise trailing slash
    dep_norm = dep.rstrip("/")
    changed_norm = changed_file.rstrip("/")

    if changed_norm == dep_norm:
        return True

    # Directory prefix match
    if changed_norm.startswith(dep_norm + "/"):
        return True

    # Original dep had trailing slash — treat as directory
    return dep.endswith("/") and changed_norm.startswith(dep_norm + "/")


def score_documents(
    entries: list[DocEntry],
    changed_files: dict[str, int],
) -> list[VolatilityResult]:
    """
    For each DocEntry, compute a VolatilityResult by matching its
    code_deps against *changed_files*.

    Returns results sorted by volatility_score descending.
    """
    results: list[VolatilityResult] = []

    for entry in entries:
        commit_hits = 0
        unique_dep_hits = 0
        touched_deps: list[str] = []
        commit_detail: dict[str, int] = {}

        for dep in entry.code_deps:
            dep_hit_count = 0
            for changed_file, commit_count in changed_files.items():
                if _dep_matches_changed_file(dep, changed_file):
                    dep_hit_count += commit_count

            if dep_hit_count > 0:
                commit_hits += dep_hit_count
                unique_dep_hits += 1
                touched_deps.append(dep)
                commit_detail[dep] = dep_hit_count

        results.append(
            VolatilityResult(
                doc_key=entry.doc_key,
                code_deps=entry.code_deps,
                commit_hits=commit_hits,
                unique_dep_hits=unique_dep_hits,
                touched_deps=sorted(touched_deps),
                commit_detail=commit_detail,
            )
        )

    results.sort(key=lambda r: (r.volatility_score, r.coverage), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

_RISK_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🟢",
    "STABLE": "⚪",
}

_RISK_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "STABLE": 4}


def build_report(
    results: list[VolatilityResult],
    since: str,
    run_date: str,
    total_commits: int,
    top_n: int | None = None,
) -> str:
    """Render a Markdown volatility report."""
    display = results[:top_n] if top_n else results

    # Summary stats
    total_docs = len(results)
    volatile_docs = sum(1 for r in results if r.volatility_score > 0)
    critical_count = sum(1 for r in results if r.risk_label() == "CRITICAL")
    high_count = sum(1 for r in results if r.risk_label() == "HIGH")

    lines: list[str] = []
    lines.append("---")
    lines.append("doc_id: doc-volatility-report")
    lines.append('title: "Documentation Volatility Report"')
    lines.append(f'generated: "{run_date}"')
    lines.append(f'since: "{since}"')
    lines.append(f"total_docs_scored: {total_docs}")
    lines.append(f"volatile_docs: {volatile_docs}")
    lines.append('schema_version: "1.0"')
    lines.append("---")
    lines.append("")
    lines.append("# Documentation Volatility Report")
    lines.append("")
    lines.append(f"> **Generated:** {run_date}  ")
    lines.append(f"> **Git window:** `--since={since}`  ")
    lines.append(f"> **Total commits in window:** {total_commits}  ")
    lines.append("> **Topology source:** `docs/doc-topology.yaml`  ")
    lines.append(f"> **Docs scored:** {total_docs}  ")
    lines.append(f"> **Docs with ≥1 volatile dep:** {volatile_docs}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Score Interpretation")
    lines.append("")
    lines.append("| Score range | Risk label | Recommended action |")
    lines.append("|-------------|------------|--------------------|")
    lines.append("| 0           | ⚪ STABLE   | No action needed — no code deps changed |")
    lines.append("| 1–3         | 🟢 LOW      | Spot-check for staleness |")
    lines.append("| 4–10        | 🟡 MEDIUM   | Schedule review within the sprint |")
    lines.append("| 11–25       | 🟠 HIGH     | Review before next release |")
    lines.append("| 26+         | 🔴 CRITICAL | Review immediately |")
    lines.append("")
    lines.append("> **Volatility score** = total number of `(code_dep, commit)` hit pairs.")
    lines.append("> A dep file touched in 3 commits = 3 points.")
    lines.append("> A directory dep matched by 4 files each touched once = 4 points.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Summary Statistics")
    lines.append("")
    lines.append(f"- **Total documents scored:** {total_docs}")
    lines.append(f"- **Documents with volatile deps:** {volatile_docs}")
    lines.append(f"- **CRITICAL documents:** {critical_count}")
    lines.append(f"- **HIGH documents:** {high_count}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Volatility Scores by Document")
    lines.append("")
    if top_n:
        lines.append(f"*Showing top {top_n} of {total_docs} documents.*")
        lines.append("")

    lines.append("| Rank | Document | Score | Risk | Deps Touched | Total Deps | Coverage |")
    lines.append("|------|----------|-------|------|-------------|------------|----------|")

    for rank, r in enumerate(display, 1):
        emoji = _RISK_EMOJI[r.risk_label()]
        label = r.risk_label()
        coverage_pct = f"{r.coverage * 100:.0f}%"
        score_str = str(r.volatility_score) if r.volatility_score > 0 else "0"
        lines.append(
            f"| {rank} | `{r.doc_key}` | {score_str} | {emoji} {label} "
            f"| {r.unique_dep_hits} | {r.total_deps} | {coverage_pct} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Document Details")
    lines.append("")
    lines.append("*Only documents with at least one volatile dependency are listed below.*")
    lines.append("")

    for r in display:
        if r.volatility_score == 0:
            continue

        emoji = _RISK_EMOJI[r.risk_label()]
        label = r.risk_label()
        lines.append(f"### `{r.doc_key}`")
        lines.append("")
        lines.append(f"- **Volatility score:** {r.volatility_score}")
        lines.append(f"- **Risk:** {emoji} {label}")
        lines.append(f"- **Deps touched / total:** {r.unique_dep_hits} / {r.total_deps}")
        lines.append(f"- **Coverage:** {r.coverage * 100:.0f}%")
        lines.append("")

        if r.commit_detail:
            lines.append("  **Changed dependencies:**")
            lines.append("")
            lines.append("  | Dependency path | Commits in window |")
            lines.append("  |-----------------|-------------------|")
            for dep, cnt in sorted(r.commit_detail.items(), key=lambda x: -x[1]):
                lines.append(f"  | `{dep}` | {cnt} |")
            lines.append("")

        untouched = [d for d in r.code_deps if d not in r.touched_deps]
        if untouched:
            lines.append("  **Stable dependencies (unchanged in window):**")
            lines.append("")
            for dep in untouched:
                lines.append(f"  - `{dep}`")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Stable Documents")
    lines.append("")
    lines.append("These documents had **no code dependency changes** in the git window.")
    lines.append("")

    stable = [r for r in results if r.volatility_score == 0]
    if stable:
        # Group: docs with deps (stable) vs. docs with no deps
        has_deps = [r for r in stable if r.total_deps > 0]
        no_deps = [r for r in stable if r.total_deps == 0]

        if has_deps:
            lines.append("**Docs with code deps that are all currently stable:**")
            lines.append("")
            for r in has_deps:
                lines.append(f"- `{r.doc_key}` ({r.total_deps} deps, all stable)")
            lines.append("")

        if no_deps:
            lines.append("**Docs with no declared code deps (topology-only):**")
            lines.append("")
            for r in no_deps:
                lines.append(f"- `{r.doc_key}`")
            lines.append("")
    else:
        lines.append("*All scored documents have at least one volatile dependency.*")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## How to Use This Report")
    lines.append("")
    lines.append("1. Focus review effort on CRITICAL and HIGH documents first.")
    lines.append(
        "2. For each volatile document, check whether the changed code deps"
        " introduced new flags, changed behavior, or removed features."
    )
    lines.append(
        "3. After reviewing, update `docs/doc-topology.yaml` if any dep relationships changed."
    )
    lines.append(
        "4. File new findings in `docs/findings-registry.md` using the next available `FR-NNN` ID."
    )
    lines.append("5. Re-run this script after fixing docs to verify the score is still meaningful.")
    lines.append("")
    lines.append(
        "_Re-run: `python scripts/doc_volatility.py --output docs/doc-volatility-report.md`_"
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="doc_volatility.py",
        description="Score documentation volatility against recent git activity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--since",
        default="3.months",
        metavar="PERIOD",
        help="git-log --since period (default: 3.months)",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="Write Markdown report to PATH (default: print to stdout)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help="Only show top N documents in the report",
    )
    parser.add_argument(
        "--topology",
        default="docs/doc-topology.yaml",
        metavar="PATH",
        help="Path to doc-topology.yaml (default: docs/doc-topology.yaml)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also emit machine-readable JSON summary to <output>.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # -- Repo root -------------------------------------------------------------
    try:
        repo_root = get_repo_root()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # -- Topology file ---------------------------------------------------------
    topology_path = repo_root / args.topology
    if not topology_path.exists():
        print(
            f"ERROR: Topology file not found: {topology_path}\n"
            "Run from repo root or pass --topology PATH",
            file=sys.stderr,
        )
        return 1

    # -- Load topology ---------------------------------------------------------
    try:
        entries = load_topology(topology_path)
    except ImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR loading topology: {exc}", file=sys.stderr)
        return 1

    print(
        f"Loaded {len(entries)} documents from {args.topology}",
        file=sys.stderr,
    )

    # -- Collect changed files -------------------------------------------------
    try:
        changed_files = collect_changed_files(repo_root, args.since)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    total_commits = _count_commits(repo_root, args.since)
    print(
        f"Git window --since={args.since}: "
        f"{len(changed_files)} unique files changed across {total_commits} commits",
        file=sys.stderr,
    )

    # -- Score -----------------------------------------------------------------
    results = score_documents(entries, changed_files)

    volatile = sum(1 for r in results if r.volatility_score > 0)
    print(
        f"Scored {len(results)} documents — {volatile} have volatile deps",
        file=sys.stderr,
    )

    # -- Report ----------------------------------------------------------------
    run_date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    report = build_report(
        results=results,
        since=args.since,
        run_date=run_date,
        total_commits=total_commits,
        top_n=args.top,
    )

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"Report written to {out_path}", file=sys.stderr)
    else:
        print(report)

    # -- Optional JSON output --------------------------------------------------
    if args.json and args.output:
        import json

        json_path = Path(args.output).with_suffix(".json")
        payload = {
            "generated": run_date,
            "since": args.since,
            "total_commits": total_commits,
            "documents": [
                {
                    "doc_key": r.doc_key,
                    "volatility_score": r.volatility_score,
                    "risk": r.risk_label(),
                    "coverage": round(r.coverage, 4),
                    "unique_dep_hits": r.unique_dep_hits,
                    "total_deps": r.total_deps,
                    "touched_deps": r.touched_deps,
                    "commit_detail": r.commit_detail,
                }
                for r in results
            ],
        }
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"JSON data written to {json_path}", file=sys.stderr)

    return 0


def _count_commits(repo_root: Path, since: str) -> int:
    """Return the number of commits in the git window."""
    try:
        raw = _run(
            ["git", "log", f"--since={since}", "--pretty=format:%H"],
            cwd=repo_root,
        )
        return len([line for line in raw.splitlines() if line.strip()])
    except RuntimeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
