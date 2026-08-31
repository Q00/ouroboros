"""Deterministic citation liveness audit for deep-tier lateral evidence.

Grounded-lateral RFC D4: a hallucinated citation under "recommendation
grounds" is the feature's biggest trust risk — one dead link and the advisory
is worse than no advisory. This module audits the URLs personas cite in their
fenced evidence blocks and reports, per URL, whether it was reachable.

Enforcement is withhold-only, mirroring the verify-gate invariant: an
unreachable or malformed citation is *marked*, never a failure of the tool
call, and a submission with no evidence block touches no network at all.
Checks are bounded twice — per-request timeout and a total wall-clock budget —
so a slow host cannot stall fan-out synthesis.

No similarity scoring and no content judgment: this gate answers exactly one
deterministic question per URL — "did this fetch succeed right now?". Whether
the source *supports* the claim stays a synthesis-level judgment.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
import re
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

VERIFIED = "verified"
UNREACHABLE = "unreachable"
INVALID = "invalid"
UNCHECKED = "unchecked"

_REQUEST_TIMEOUT_SECONDS = 4.0
_TOTAL_BUDGET_SECONDS = 10.0
_MAX_URLS = 8
_MAX_URL_LEN = 2000

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_cited_urls(text: str) -> tuple[str, ...]:
    """Extract cited URLs from the fenced evidence JSON block(s) in ``text``.

    Reads the deep-tier contract shape written by
    ``build_lateral_multi_subagent``: ``external_sources`` (list of URLs) and
    ``claims[].source``. Order-preserving, deduplicated. Any malformed block
    is skipped rather than raised — a persona that broke the format simply
    contributes no checkable citations.
    """
    if not text:
        return ()
    urls: list[str] = []
    seen: set[str] = set()
    for match in _FENCED_JSON_RE.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        candidates: list[Any] = []
        sources = payload.get("external_sources")
        if isinstance(sources, list):
            candidates.extend(sources)
        claims = payload.get("claims")
        if isinstance(claims, list):
            candidates.extend(claim.get("source") for claim in claims if isinstance(claim, Mapping))
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            url = candidate.strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return tuple(urls)


def _default_fetch(url: str, timeout: float) -> bool:
    """One bounded liveness probe. True iff the fetch completed with 2xx/3xx.

    HEAD first (cheapest); a server that rejects HEAD outright (405/501) gets
    one ranged GET so it is not falsely marked dead. Anything else — DNS
    failure, TLS failure, timeout, 4xx/5xx — is "not alive right now".
    """
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        request = urllib.request.Request(  # noqa: S310 - scheme validated by caller
            url,
            method=method,
            headers={"User-Agent": "ouroboros-citation-check/1", **headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310
                return True
        except urllib.error.HTTPError as error:
            if method == "HEAD" and error.code in (405, 501):
                continue  # server refuses HEAD; try the ranged GET once
            return 200 <= error.code < 400
        except Exception:
            return False
    return False


def audit_citations(
    texts: Iterable[str],
    *,
    fetch: Callable[[str, float], bool] = _default_fetch,
    total_budget_seconds: float = _TOTAL_BUDGET_SECONDS,
    max_urls: int = _MAX_URLS,
) -> dict[str, Any] | None:
    """Audit every citation found in ``texts``; None when nothing was cited.

    Returns ``{"checked": n, "urls": {url: verdict}, "unverified_present":
    bool}`` where each verdict is one of ``verified`` / ``unreachable`` /
    ``invalid`` (non-http(s) or oversized URL — never fetched) / ``unchecked``
    (budget or count cap reached before this URL's turn). Never raises.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for url in extract_cited_urls(text if isinstance(text, str) else ""):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    if not urls:
        return None

    verdicts: dict[str, str] = {}
    deadline = time.monotonic() + total_budget_seconds
    checked = 0
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or len(url) > _MAX_URL_LEN:
            verdicts[url] = INVALID
            continue
        if checked >= max_urls or time.monotonic() >= deadline:
            verdicts[url] = UNCHECKED
            continue
        remaining = min(_REQUEST_TIMEOUT_SECONDS, deadline - time.monotonic())
        try:
            alive = bool(fetch(url, max(remaining, 0.1)))
        except Exception:
            alive = False
        verdicts[url] = VERIFIED if alive else UNREACHABLE
        checked += 1

    return {
        "checked": checked,
        "urls": verdicts,
        "unverified_present": any(v != VERIFIED for v in verdicts.values()),
        "rule": (
            "Withhold-only: cite 'verified' URLs as grounds; render any other "
            "verdict as 'unverified' or drop the citation. Never present an "
            "unverified source as authority."
        ),
    }


__all__ = [
    "INVALID",
    "UNCHECKED",
    "UNREACHABLE",
    "VERIFIED",
    "audit_citations",
    "extract_cited_urls",
]
