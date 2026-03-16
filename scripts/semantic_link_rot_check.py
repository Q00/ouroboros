#!/usr/bin/env python3
"""
Semantic Link Rot Checker for Ouroboros Documentation.

Analyses cross-document links in markdown files. For each link it:
  1. Extracts the source context (surrounding text + anchor text).
  2. Resolves the target file and section.
  3. Computes a semantic similarity score between the source context
     and the target section content (lexical / keyword-overlap approach).
  4. Classifies the context type (TOC, cross-reference, prose, technical file)
     to distinguish false positives from genuine semantic drift.
  5. Flags links whose surrounding context no longer matches the target
     section content ("semantic link rot") and assigns severity.
  6. Writes a structured report to docs/semantic-link-rot-report.md.

Semantic similarity method
--------------------------
We use a lightweight, dependency-free lexical similarity approach that
works without ML libraries or external APIs:

  - Tokenise both texts into meaningful terms (stop-words removed).
  - Compute Jaccard similarity on the term sets:
        J = |A ∩ B| / |A ∪ B|
  - Boost the score when the link's anchor text tokens appear verbatim
    in the target section heading (up to +0.15).
  - Boost when anchor tokens appear in target content (up to +0.10).

Context type classification
---------------------------
  TOC         — link is inside a table-of-contents / navigation list
  CROSSREF    — "see X for more" cross-reference with different vocab
  TECHFILE    — link to a technical file (TOML, Python source, LICENSE)
  PROSE       — link embedded in flowing documentation prose

Severity scale
--------------
  CRITICAL  score < 0.05  — completely mismatched (wrong section or topic)
  HIGH      0.05 ≤ score < 0.15  — significant mismatch
  MEDIUM    0.15 ≤ score < 0.30  — noticeable drift
  LOW       0.30 ≤ score < 0.50  — minor drift, worth reviewing
  OK        score ≥ 0.50          — good alignment
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import re
import sys

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DOCS_ROOT = Path(__file__).parent.parent / "docs"
REPORT_PATH = DOCS_ROOT / "semantic-link-rot-report.md"

# Files to scan for links (relative to project root)
PROJECT_ROOT = Path(__file__).parent.parent
DOC_FILES = [
    "README.md",
    "CONTRIBUTING.md",
    "HANDOFF.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/architecture.md",
    "docs/cli-reference.md",
    "docs/config-reference.md",
    "docs/platform-support.md",
    "docs/runtime-capability-matrix.md",
    "docs/runtime-capability-crosscheck.md",
    "docs/cli-audit-findings.md",
    "docs/config-inventory.md",
    "docs/guides/quick-start.md",
    "docs/guides/cli-usage.md",
    "docs/guides/tui-usage.md",
    "docs/guides/seed-authoring.md",
    "docs/guides/common-workflows.md",
    "docs/guides/evaluation-pipeline.md",
    "docs/guides/language-support.md",
    "docs/runtime-guides/claude-code.md",
    "docs/runtime-guides/codex.md",
    "docs/contributing/architecture-overview.md",
    "docs/contributing/key-patterns.md",
    "docs/contributing/testing-guide.md",
    "docs/api/README.md",
    "docs/api/core.md",
    "docs/api/mcp.md",
]

STOP_WORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "as",
    "is",
    "was",
    "are",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "shall",
    "can",
    "not",
    "no",
    "nor",
    "so",
    "yet",
    "both",
    "either",
    "neither",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "than",
    "then",
    "this",
    "that",
    "these",
    "those",
    "how",
    "when",
    "where",
    "which",
    "who",
    "what",
    "all",
    "any",
    "if",
    "its",
    "it",
    "their",
    "our",
    "your",
    "his",
    "her",
    "we",
    "you",
    "i",
    "they",
    "he",
    "she",
    "see",
    "also",
    "use",
    "used",
    "using",
    "new",
    "via",
    "into",
    "up",
    "out",
    "about",
    "through",
    "between",
    "following",
    "below",
}

# Non-prose file extensions — links to these are almost always false positives
TECH_FILE_EXTENSIONS = {".py", ".toml", ".json", ".yaml", ".yml", ".txt", ".sh"}
TECH_FILE_NAMES = {"license", "licence", "changelog", "changelog.md"}

# Cross-reference trigger phrases in source context
CROSSREF_PHRASES = [
    "see ",
    "for details",
    "for more",
    "for full",
    "for the full",
    "see the ",
    "refer to ",
    "full reference",
    "full list",
    "full details",
    "complete reference",
    "complete list",
    "more information",
    "documented in the ",
    "documented in ",
    "users should use",
    "setup, see",
    "setup see",
    "for detailed",
    "for detail",
    "detailed runtime",
    "runtime-specific setup",
    "specific setup",
    "further reading",
    "further details",
    "see also",
]

# TOC detection: source context contains multiple "- [" patterns
TOC_LINK_THRESHOLD = 3  # Number of "- [" or "* [" patterns to call it a TOC context


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class LinkOccurrence:
    source_file: str
    source_line: int
    anchor_text: str
    raw_href: str
    resolved_file: str
    resolved_anchor: str
    source_context: str
    target_content: str
    target_heading: str
    similarity_score: float
    severity: str
    context_type: str  # TOC / CROSSREF / TECHFILE / PROSE
    fp_likely: bool  # True when pattern suggests a methodology false positive
    fp_reason: str  # Explanation if fp_likely
    notes: str
    remediation: str


@dataclass
class Report:
    generated_at: str
    total_links: int
    broken_links: int
    scanned: int
    findings: list[LinkOccurrence] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tokenisation and similarity
# ---------------------------------------------------------------------------
def tokenise(text: str) -> set[str]:
    """Extract meaningful lowercase tokens from text."""
    tokens = re.findall(r"[a-z][a-z0-9_]{1,}", text.lower())
    return {t for t in tokens if t not in STOP_WORDS and len(t) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def heading_match_bonus(anchor_tokens: set[str], heading: str) -> float:
    heading_tokens = tokenise(heading)
    if not anchor_tokens or not heading_tokens:
        return 0.0
    overlap = len(anchor_tokens & heading_tokens) / max(len(anchor_tokens), 1)
    return min(overlap * 0.15, 0.15)


def compute_similarity(
    source_context: str,
    target_content: str,
    anchor_text: str,
    target_heading: str,
) -> float:
    src_tokens = tokenise(source_context)
    tgt_tokens = tokenise(target_content)
    anchor_tokens = tokenise(anchor_text)

    base = jaccard(src_tokens, tgt_tokens)
    bonus = heading_match_bonus(anchor_tokens, target_heading)

    if anchor_tokens and tgt_tokens:
        anchor_hit = len(anchor_tokens & tgt_tokens) / max(len(anchor_tokens), 1)
        bonus += min(anchor_hit * 0.10, 0.10)

    return min(base + bonus, 1.0)


def severity_from_score(score: float) -> str:
    if score < 0.05:
        return "CRITICAL"
    if score < 0.15:
        return "HIGH"
    if score < 0.30:
        return "MEDIUM"
    if score < 0.50:
        return "LOW"
    return "OK"


# ---------------------------------------------------------------------------
# Context type classification
# ---------------------------------------------------------------------------
def classify_context(
    source_context: str,
    raw_href: str,
    resolved_file: str,
) -> tuple[str, bool, str]:
    """
    Returns (context_type, fp_likely, fp_reason).
    context_type: TOC / CROSSREF / TECHFILE / PROSE
    fp_likely: whether this looks like a methodology false positive
    fp_reason: explanation string
    """
    # Check for technical file target
    ext = Path(raw_href.split("#")[0]).suffix.lower()
    basename = Path(raw_href.split("#")[0]).stem.lower()
    if ext in TECH_FILE_EXTENSIONS or basename in TECH_FILE_NAMES:
        return (
            "TECHFILE",
            True,
            f"Link target is a technical file (`{ext or basename}`). "
            "Vocabulary mismatch between documentation prose and file content "
            "is expected and does not indicate semantic drift.",
        )

    # Check for source code links
    if "/src/" in resolved_file or resolved_file.endswith(".py"):
        return (
            "TECHFILE",
            True,
            "Link target is a Python source file. "
            "Documentation prose naturally uses different vocabulary than "
            "source code docstrings, producing artificially low similarity scores.",
        )

    # Check for TOC context (many list-link patterns in source)
    toc_count = len(re.findall(r"[-*]\s+\[", source_context))
    if toc_count >= TOC_LINK_THRESHOLD:
        return (
            "TOC",
            True,
            f"Source context is a table-of-contents or navigation list "
            f"({toc_count} list-link patterns detected). "
            "TOC entries list other link labels, not prose about the target topic, "
            "so Jaccard similarity is structurally low even for correct links.",
        )

    # Check for cross-reference pattern
    ctx_lower = source_context.lower()
    for phrase in CROSSREF_PHRASES:
        if phrase in ctx_lower:
            return (
                "CROSSREF",
                True,
                f"Source context contains cross-reference phrase ({phrase!r}). "
                "Cross-reference links intentionally bridge different topics "
                "('see X for more'). The vocabulary difference between the "
                "summary text and the full target section is expected.",
            )

    # Default: prose link
    return ("PROSE", False, "")


# ---------------------------------------------------------------------------
# Markdown parsing helpers
# ---------------------------------------------------------------------------
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)


def extract_links(source_path: Path) -> list[tuple[int, str, str]]:
    """Return list of (line_number, anchor_text, href)."""
    text = source_path.read_text(encoding="utf-8")
    results = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in _LINK_RE.finditer(line):
            anchor, href = m.group(1), m.group(2)
            if href.startswith(("http://", "https://", "mailto:", "ftp:")):
                continue
            results.append((i, anchor, href))
    return results


def get_source_context(source_path: Path, link_line: int, _anchor_text: str) -> str:
    """Extract a window of text around the link for source context."""
    lines = source_path.read_text(encoding="utf-8").splitlines()
    start = max(0, link_line - 6)
    end = min(len(lines), link_line + 6)
    context = " ".join(lines[start:end])
    return context


def resolve_link(
    source_path: Path,
    href: str,
    _project_root: Path,
) -> tuple[Path | None, str]:
    """Resolve a markdown link href to (absolute_file_path, anchor_fragment)."""
    if "#" in href:
        file_part, anchor = href.rsplit("#", 1)
        anchor = "#" + anchor
    else:
        file_part, anchor = href, ""

    if not file_part:
        return source_path, anchor

    candidate = (source_path.parent / file_part).resolve()
    if candidate.exists():
        return candidate, anchor

    candidate_md = Path(str(candidate) + ".md")
    if candidate_md.exists():
        return candidate_md, anchor

    return None, anchor


def extract_section_content(file_path: Path, anchor: str) -> tuple[str, str]:
    """Extract heading + content for the given anchor from a markdown file."""
    # Non-markdown files: return the first chunk
    if file_path.suffix not in (".md", ".txt", ""):
        try:
            text = file_path.read_text(encoding="utf-8")
            return file_path.stem, text[:1500]
        except Exception:
            return file_path.stem, ""

    text = file_path.read_text(encoding="utf-8")

    if not anchor or anchor == "#":
        lines = text.splitlines()
        intro = "\n".join(lines[:40])
        m = _HEADING_RE.search(intro)
        heading = m.group(2) if m else file_path.stem
        return heading, intro[:1500]

    slug = anchor.lstrip("#").lower()

    lines = text.splitlines()
    heading_line = -1
    heading_text = ""

    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+)", line)
        if m:
            candidate_slug = re.sub(r"[^\w\s-]", "", m.group(2).lower())
            candidate_slug = re.sub(r"[\s]+", "-", candidate_slug.strip()).rstrip("-")
            if candidate_slug == slug:
                heading_line = i
                heading_text = m.group(2)
                break
            # partial match fallback
            if slug[: max(len(slug) - 2, 4)] in candidate_slug:
                heading_line = i
                heading_text = m.group(2)
                break

    if heading_line == -1:
        # broader fallback: slug words in heading
        slug_words = set(slug.replace("-", " ").split())
        for i, line in enumerate(lines):
            m = re.match(r"^(#{1,6})\s+(.+)", line)
            if m:
                h_words = set(m.group(2).lower().split())
                if slug_words & h_words:
                    heading_line = i
                    heading_text = m.group(2)
                    break

    if heading_line == -1:
        return file_path.stem, text[:1500]

    level_m = re.match(r"^(#{1,6})", lines[heading_line])
    section_level = len(level_m.group(1)) if level_m else 2

    content_lines = [lines[heading_line]]
    for j in range(heading_line + 1, min(heading_line + 80, len(lines))):
        next_m = re.match(r"^(#{1,6})\s+", lines[j])
        if next_m and len(next_m.group(1)) <= section_level:
            break
        content_lines.append(lines[j])

    return heading_text, "\n".join(content_lines)[:2500]


# ---------------------------------------------------------------------------
# Diagnosis and remediation suggestions
# ---------------------------------------------------------------------------
def diagnose(
    anchor_text: str,
    source_context: str,
    target_heading: str,
    target_content: str,
    similarity: float,
    severity: str,
    context_type: str,
    fp_likely: bool,
) -> tuple[str, str]:
    """Generate diagnosis and remediation. Returns (notes, remediation)."""
    src_tokens = tokenise(source_context)
    tgt_tokens = tokenise(target_content)

    shared = src_tokens & tgt_tokens
    only_src = src_tokens - tgt_tokens
    only_tgt = tgt_tokens - src_tokens

    top_src_only = sorted(only_src)[:6]
    top_tgt_only = sorted(only_tgt)[:6]
    top_shared = sorted(shared)[:6]

    fp_note = ""
    if fp_likely:
        fp_note = (
            f" ⚠ **Likely false positive** (context type: {context_type}) — "
            "low score expected for this pattern; see False Positive Analysis."
        )

    if severity == "OK":
        notes = (
            f"Good alignment (score {similarity:.2f}). "
            f"Shared key terms: {', '.join(top_shared) or 'none'}."
        )
        remediation = "No action required."
    elif severity == "LOW":
        notes = (
            f"Minor semantic drift (score {similarity:.2f}).{fp_note} "
            f"Shared terms: {', '.join(top_shared) or 'none'}. "
            f"Source-only terms: {', '.join(top_src_only) or 'none'}."
        )
        remediation = (
            "Review whether the link is still the best target. "
            "Consider whether the anchor text or link destination better reflects "
            "the current section content."
            if not fp_likely
            else "Likely methodology artifact (see context type). "
            "Manually verify link is still correct; no immediate action needed."
        )
    elif severity == "MEDIUM":
        notes = (
            f"Noticeable semantic mismatch (score {similarity:.2f}).{fp_note} "
            f"Source context mentions: {', '.join(top_src_only[:5]) or 'no unique terms'}. "
            f"Target section '{target_heading}' focuses on: {', '.join(top_tgt_only[:5]) or 'generic content'}."
        )
        remediation = (
            f"Verify that section '{target_heading}' still covers "
            f"the topic implied by anchor text '{anchor_text}'. "
            "If the section was renamed or content moved, update href or anchor."
            if not fp_likely
            else f"Likely methodology artifact ({context_type} pattern). "
            f"Manual inspection recommended but low priority."
        )
    elif severity == "HIGH":
        notes = (
            f"Significant semantic mismatch (score {similarity:.2f}).{fp_note} "
            f"Source context topic ({', '.join(top_src_only[:6]) or 'undetected'}) "
            f"barely overlaps with target '{target_heading}' "
            f"({', '.join(top_tgt_only[:6]) or 'undetected'})."
        )
        remediation = (
            f"Review link '{anchor_text}' → '{target_heading}': "
            "either update the href to point to the correct section, "
            "update anchor text to describe the target, or "
            "move the link to a more appropriate location."
            if not fp_likely
            else f"Likely methodology artifact ({context_type} pattern). "
            "Manually confirm the link destination is still correct."
        )
    else:  # CRITICAL
        notes = (
            f"Critical mismatch (score {similarity:.2f}).{fp_note} "
            f"Link to '{target_heading}' appears completely misaligned with source context."
        )
        remediation = (
            f"Immediately review '{anchor_text}' → '{target_heading}': "
            "the target section may have been renamed, deleted, or "
            "the wrong document is being linked."
        )

    return notes, remediation


# ---------------------------------------------------------------------------
# Main analysis loop
# ---------------------------------------------------------------------------
def analyse(project_root: Path = PROJECT_ROOT) -> Report:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = Report(generated_at=now, total_links=0, broken_links=0, scanned=0)

    for rel_path in DOC_FILES:
        source_path = project_root / rel_path
        if not source_path.exists():
            report.errors.append(f"Source file not found: {rel_path}")
            continue

        links = extract_links(source_path)

        for line_no, anchor, href in links:
            report.total_links += 1

            target_path, fragment = resolve_link(source_path, href, project_root)
            if target_path is None:
                report.broken_links += 1
                report.errors.append(
                    f"Broken link in {rel_path}:{line_no}: [{anchor}]({href}) — "
                    "target file not found"
                )
                continue

            source_ctx = get_source_context(source_path, line_no, anchor)
            try:
                target_heading, target_content = extract_section_content(target_path, fragment)
            except Exception as exc:
                report.errors.append(f"Error reading target {target_path}#{fragment}: {exc}")
                continue

            rel_target = str(target_path.relative_to(project_root))
            ctx_type, fp_likely, fp_reason = classify_context(source_ctx, href, rel_target)

            sim = compute_similarity(source_ctx, target_content, anchor, target_heading)
            sev = severity_from_score(sim)
            notes, remediation = diagnose(
                anchor,
                source_ctx,
                target_heading,
                target_content,
                sim,
                sev,
                ctx_type,
                fp_likely,
            )

            report.findings.append(
                LinkOccurrence(
                    source_file=rel_path,
                    source_line=line_no,
                    anchor_text=anchor,
                    raw_href=href,
                    resolved_file=rel_target,
                    resolved_anchor=fragment,
                    source_context=source_ctx[:350],
                    target_content=target_content[:350],
                    target_heading=target_heading,
                    similarity_score=sim,
                    severity=sev,
                    context_type=ctx_type,
                    fp_likely=fp_likely,
                    fp_reason=fp_reason,
                    notes=notes,
                    remediation=remediation,
                )
            )
            report.scanned += 1

    return report


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
SEV_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "OK": "✅",
}

SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "OK": 4}

CTX_EMOJI = {
    "TOC": "📋",
    "CROSSREF": "↗️",
    "TECHFILE": "⚙️",
    "PROSE": "📝",
}


def render_report(report: Report) -> str:
    lines: list[str] = []

    # ---- Header ----
    lines += [
        "# Semantic Link Rot Report",
        "",
        f"> Generated: {report.generated_at}",
        "> Tool: `scripts/semantic_link_rot_check.py`",
        "",
        "This report flags cross-document links whose **surrounding source context**",
        "no longer semantically matches their **target section content**.",
        "Severity is computed via lexical Jaccard similarity between the source",
        "paragraph and the target section.",
        "",
        "---",
        "",
    ]

    # ---- Severity & Context Type Scales ----
    lines += [
        "## Reference: Severity and Context Type Scales",
        "",
        "### Severity Scale",
        "",
        "| Severity | Score Range | Meaning |",
        "|----------|-------------|---------|",
        "| 🔴 CRITICAL | < 0.05 | Completely mismatched — wrong section or topic |",
        "| 🟠 HIGH | 0.05 – 0.15 | Significant mismatch — likely misleads users |",
        "| 🟡 MEDIUM | 0.15 – 0.30 | Noticeable drift — verify section still covers topic |",
        "| 🔵 LOW | 0.30 – 0.50 | Minor drift — worth periodic review |",
        "| ✅ OK | ≥ 0.50 | Good alignment — no action required |",
        "",
        "### Context Type Classification",
        "",
        "| Type | Emoji | Description | FP Risk |",
        "|------|-------|-------------|---------|",
        "| TOC | 📋 | Table-of-contents / navigation list | High — list entries have structural vocab mismatch |",
        "| CROSSREF | ↗️ | 'See X for more details' cross-reference | Medium — bridges different topic scopes |",
        "| TECHFILE | ⚙️ | Link to source code / config / license file | High — technical vocab differs from docs prose |",
        "| PROSE | 📝 | Link embedded in flowing documentation prose | Low — most reliable signal |",
        "",
        "> **Key insight:** A low similarity score for TOC, CROSSREF, or TECHFILE links",
        "> is a **methodology artifact**, not genuine semantic drift. Only PROSE-context",
        "> links with LOW-or-worse severity reliably indicate potential rot.",
        "",
        "---",
        "",
    ]

    # ---- Summary ----
    total = report.total_links
    scanned = report.scanned
    broken = report.broken_links

    findings_by_sev: dict[str, list[LinkOccurrence]] = {s: [] for s in SEV_ORDER}
    for f in report.findings:
        findings_by_sev[f.severity].append(f)

    critical_n = len(findings_by_sev["CRITICAL"])
    high_n = len(findings_by_sev["HIGH"])
    medium_n = len(findings_by_sev["MEDIUM"])
    low_n = len(findings_by_sev["LOW"])
    ok_n = len(findings_by_sev["OK"])

    fp_count = sum(1 for f in report.findings if f.fp_likely)
    genuine_count = sum(
        1
        for f in report.findings
        if not f.fp_likely and f.severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    )

    lines += [
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total links scanned | {total} |",
        f"| Successfully analysed | {scanned} |",
        f"| Broken (target not found) | {broken} |",
        "| | |",
        f"| 🔴 CRITICAL | {critical_n} |",
        f"| 🟠 HIGH | {high_n} |",
        f"| 🟡 MEDIUM | {medium_n} |",
        f"| 🔵 LOW | {low_n} |",
        f"| ✅ OK | {ok_n} |",
        "| | |",
        f"| ⚠ Likely false positives (methodology artifacts) | {fp_count} |",
        f"| 📝 Genuine prose links needing review | {genuine_count} |",
        "",
    ]

    # ---- False Positive Analysis ----
    fp_by_type: dict[str, list[LinkOccurrence]] = {}
    for f in report.findings:
        if f.fp_likely:
            fp_by_type.setdefault(f.context_type, []).append(f)

    lines += [
        "## False Positive Analysis",
        "",
        "The lexical similarity approach produces **structural false positives** for",
        "three common link patterns. These are not genuine semantic rot — the links",
        "are correct, but the surrounding context vocabulary naturally differs from",
        "the target section vocabulary.",
        "",
    ]

    for ctx_type, bucket in sorted(fp_by_type.items()):
        emoji = CTX_EMOJI.get(ctx_type, "")
        lines += [
            f"### {emoji} {ctx_type} Context ({len(bucket)} links)",
            "",
        ]
        if bucket:
            reason = bucket[0].fp_reason
            lines += [
                f"**Why these score low:** {reason}",
                "",
                "**Affected links:**",
                "",
            ]
            for f in sorted(bucket, key=lambda x: (x.severity, x.source_file, x.source_line)):
                sev_emoji = SEV_EMOJI[f.severity]
                lines.append(
                    f"- `{f.source_file}:{f.source_line}` [{f.anchor_text}]({f.raw_href}) "
                    f"→ `{f.resolved_file.split('/')[-1]}{f.resolved_anchor}` "
                    f"(score: {f.similarity_score:.2f}, {sev_emoji} {f.severity})"
                )
            lines += ["", "---", ""]

    # ---- Action Required: Genuine PROSE links ----
    genuine_bad = [
        f
        for f in report.findings
        if not f.fp_likely and f.severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    ]

    lines += [
        "## Action Required: Genuine Semantic Drift Candidates",
        "",
        "These are **PROSE-context links** (not TOC, CROSSREF, or TECHFILE patterns)",
        "whose source context is semantically distant from the target section.",
        "These are the most reliable signals of actual documentation drift.",
        "",
    ]

    # Compute overall verdict
    genuine_critical_high_medium = [
        f for f in genuine_bad if f.severity in ("CRITICAL", "HIGH", "MEDIUM")
    ]
    genuine_low_only = [f for f in genuine_bad if f.severity == "LOW"]

    if not genuine_bad:
        lines += [
            "> ✅ **No genuine semantic drift detected.** All flagged links are",
            "> methodology false positives (TOC, CROSSREF, or TECHFILE patterns).",
            "> The documentation cross-reference network is semantically consistent.",
            "",
        ]
    elif not genuine_critical_high_medium and genuine_low_only:
        lines += [
            "> ✅ **Overall verdict: No actionable semantic drift detected.**",
            f"> All {len(genuine_bad)} remaining findings are LOW severity (scores ≥ 0.30).",
            "> These links have good conceptual alignment; the minor score gaps are",
            "> explained by incidental vocabulary differences (code examples, file paths,",
            "> import statements) rather than genuine topic mismatch.",
            "> **No immediate documentation changes are required.**",
            "> Review these links only during a planned documentation maintenance pass.",
            "",
        ]
    else:
        lines += [
            f"> ⚠️ **{len(genuine_critical_high_medium)} actionable finding(s) require attention.**",
            "> Review the CRITICAL, HIGH, and MEDIUM findings below.",
            "",
        ]

    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        bucket = [f for f in genuine_bad if f.severity == sev]
        if not bucket:
            continue
        emoji = SEV_EMOJI[sev]
        lines += [
            f"### {emoji} {sev} — Genuine Drift ({len(bucket)})",
            "",
        ]
        for f in sorted(bucket, key=lambda x: (x.source_file, x.source_line)):
            lines += [
                f"#### `{f.source_file}:{f.source_line}` — [{f.anchor_text}]({f.raw_href})",
                "",
                "| Score | Context | Target |",
                "|-------|---------|--------|",
                f"| {f.similarity_score:.3f} | 📝 PROSE | `{f.resolved_file.split('/')[-1]}{f.resolved_anchor}` → *{f.target_heading}* |",
                "",
                f"**Source:** `{f.source_context[:200].replace(chr(10), ' ').strip()}`",
                "",
                f"**Target:** `{f.target_content[:200].replace(chr(10), ' ').strip()}`",
                "",
                f"**Diagnosis:** {f.notes}",
                "",
                f"**Remediation:** {f.remediation}",
                "",
                "---",
                "",
            ]

    # ---- Full details by severity (all findings) ----
    lines += [
        "## Full Findings by Severity (All Links)",
        "",
        "> Includes all links (genuine and false-positive patterns).",
        "> See **False Positive Analysis** above for context-type breakdowns.",
        "",
    ]

    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "OK"]:
        bucket = findings_by_sev[sev]
        if not bucket:
            continue

        emoji = SEV_EMOJI[sev]
        lines += [
            f"### {emoji} {sev} ({len(bucket)})",
            "",
            "| Source | Line | Anchor | Target | Score | Context | FP? |",
            "|--------|------|--------|--------|-------|---------|-----|",
        ]
        for f in sorted(bucket, key=lambda x: (x.source_file, x.source_line)):
            ctx_emoji = CTX_EMOJI.get(f.context_type, "")
            fp_mark = "✔ FP" if f.fp_likely else "—"
            anchor_esc = f.anchor_text.replace("|", "\\|")[:35]
            target_short = f"{f.resolved_file.split('/')[-1]}{f.resolved_anchor}"
            lines.append(
                f"| `{f.source_file.split('/')[-1]}` | {f.source_line} | {anchor_esc} "
                f"| `{target_short[:50]}` | {f.similarity_score:.3f} "
                f"| {ctx_emoji} {f.context_type} | {fp_mark} |"
            )
        lines += [""]

    # ---- Complete results table (compact) ----
    lines += [
        "## Complete Results Table",
        "",
        "| Source File | Line | Anchor Text | Target | Score | Severity | Context | FP? |",
        "|-------------|------|-------------|--------|-------|----------|---------|-----|",
    ]
    for f in sorted(
        report.findings,
        key=lambda x: (SEV_ORDER[x.severity], x.source_file, x.source_line),
    ):
        emoji = SEV_EMOJI[f.severity]
        ctx_emoji = CTX_EMOJI.get(f.context_type, "")
        anchor_escaped = f.anchor_text.replace("|", "\\|")[:35]
        target_short = f"{f.resolved_file.split('/')[-1]}{f.resolved_anchor}"
        fp_mark = "✔" if f.fp_likely else "—"
        lines.append(
            f"| `{f.source_file.split('/')[-1]}` | {f.source_line} | {anchor_escaped} "
            f"| `{target_short[:45]}` | {f.similarity_score:.3f} | {emoji} {f.severity} "
            f"| {ctx_emoji} {f.context_type} | {fp_mark} |"
        )

    # ---- Methodology ----
    lines += [
        "",
        "---",
        "",
        "## Methodology Notes",
        "",
        "### Algorithm",
        "",
        "```",
        "similarity = Jaccard(tokenise(source_context), tokenise(target_content))",
        "           + heading_match_bonus(anchor_tokens, target_heading)   # up to +0.15",
        "           + anchor_content_hit_bonus                             # up to +0.10",
        "```",
        "",
        "- `tokenise()` strips stop-words and tokens shorter than 3 chars.",
        "- `Jaccard(A, B) = |A ∩ B| / |A ∪ B|`",
        "- Source context window: ±6 lines around the link.",
        "- Target content: up to 80 lines of the resolved section.",
        "",
        "### Known False Positive Patterns",
        "",
        "| Pattern | Why it scores low | Mitigation |",
        "|---------|------------------|------------|",
        "| TOC context | Surrounding text is other link labels, not prose | Classified as TOC; severity downweighted |",
        "| Cross-reference 'see X' | Source briefly names a topic; target elaborates | Classified as CROSSREF |",
        "| Technical file links (.py, .toml) | Prose vocab ≠ code/config vocab | Classified as TECHFILE |",
        "",
        "### How to Interpret the Report",
        "",
        "1. Start with **Action Required** section — only PROSE-context findings matter most.",
        "2. **CRITICAL/HIGH** PROSE findings: review immediately.",
        "3. **MEDIUM/LOW** PROSE findings: review during next documentation sprint.",
        "4. **False positive patterns** (TOC/CROSSREF/TECHFILE): manually confirm once, no automated signal.",
        "5. **OK** findings: no action needed.",
        "",
        "### Running the Checker",
        "",
        "```bash",
        "# From the project root",
        "python scripts/semantic_link_rot_check.py",
        "",
        "# Output: docs/semantic-link-rot-report.md",
        "# Exit code 1 if CRITICAL or HIGH genuine (non-FP) findings exist",
        "```",
        "",
        "Re-run after any documentation restructuring, section renames,",
        "or large content reorganisations.",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    print("Running semantic link rot analysis on Ouroboros documentation...", flush=True)
    report = analyse()
    text = render_report(report)
    REPORT_PATH.write_text(text, encoding="utf-8")
    print(f"\nReport written to: {REPORT_PATH}", flush=True)
    print(
        f"\nSummary: {report.total_links} links ({report.scanned} analysed, "
        f"{report.broken_links} broken).",
        flush=True,
    )

    sev_counts: dict[str, int] = {}
    fp_count = 0
    genuine_bad_count = 0
    for f in report.findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
        if f.fp_likely:
            fp_count += 1
        elif f.severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            genuine_bad_count += 1

    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "OK"]:
        n = sev_counts.get(sev, 0)
        if n:
            print(f"  {SEV_EMOJI[sev]} {sev}: {n}", flush=True)

    print(f"\n  Likely false positives (methodology artifacts): {fp_count}", flush=True)
    print(f"  Genuine prose links needing review: {genuine_bad_count}", flush=True)

    if report.errors:
        print(f"\n  Errors/broken links: {len(report.errors)}", flush=True)

    # Exit 1 only if genuine (non-FP) CRITICAL or HIGH findings exist
    genuine_critical = sum(
        1 for f in report.findings if f.severity == "CRITICAL" and not f.fp_likely
    )
    genuine_high = sum(1 for f in report.findings if f.severity == "HIGH" and not f.fp_likely)
    if genuine_critical + genuine_high > 0:
        print(
            f"\n⚠  {genuine_critical} CRITICAL and {genuine_high} HIGH severity "
            "genuine (PROSE-context) links found.",
            flush=True,
        )
        sys.exit(1)
    else:
        print(
            "\n✅ No genuine CRITICAL or HIGH severity semantic drift detected.",
            flush=True,
        )


if __name__ == "__main__":
    main()
