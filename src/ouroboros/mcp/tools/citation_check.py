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
import ipaddress
import json
import re
import string
import time
from typing import Any
import unicodedata
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
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_SUB_DELIMS = frozenset("!$&'()*+,;=")
_REG_NAME_CHARS = _UNRESERVED | _SUB_DELIMS
_PCHAR = _REG_NAME_CHARS | frozenset(":@")
_HEX = frozenset(string.hexdigits)


def _uri_component_is_syntactic(value: str, allowed: frozenset[str]) -> bool:
    """Recognize URI delimiters, safe IRI characters, and complete percent triplets."""
    index = 0
    while index < len(value):
        if value[index] == "%":
            if index + 2 >= len(value) or not all(
                digit in _HEX for digit in value[index + 1 : index + 3]
            ):
                return False
            index += 3
        elif value[index] in allowed or (
            ord(value[index]) > 127
            and not value[index].isspace()
            and not unicodedata.category(value[index]).startswith("C")
        ):
            index += 1
        else:
            return False
    return True


def _eligible_http_citation(url: str) -> bool:
    """Fail closed on malformed URI syntax before URL budgets or fetches."""
    if len(url) > _MAX_URL_LEN or any(
        char.isspace() or unicodedata.category(char).startswith("C") for char in url
    ):
        return False
    try:
        # urlsplit delegates bracket literals to ipaddress, which rejects a
        # valid percent-encoded ZoneID. Parse with the bare address, then
        # validate the original authority and every URI component below.
        authority_match = re.match(r"(?i)^https?://([^/?#]*)", url)
        raw_authority = authority_match.group(1) if authority_match else None
        parse_url = url
        if authority_match and raw_authority is not None:
            opening = raw_authority.find("[")
            closing = raw_authority.find("]", opening + 1)
            if opening >= 0 and closing > opening:
                literal = raw_authority[opening + 1 : closing]
                if "%" in literal:
                    bare = literal.split("%", 1)[0]
                    offset = authority_match.start(1)
                    parse_url = url[: offset + opening + 1] + bare + url[offset + closing :]
        parsed = urllib.parse.urlsplit(parse_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        authority = raw_authority if raw_authority is not None else parsed.netloc
        if "@" in authority:
            if authority.count("@") != 1:
                return False
            userinfo, authority = authority.split("@", 1)
            if not _uri_component_is_syntactic(userinfo, _REG_NAME_CHARS | frozenset(":")):
                return False
        if authority.startswith("["):
            closing = authority.find("]")
            if closing < 0:
                return False
            literal = authority[1:closing]
            if not _uri_component_is_syntactic(literal, _REG_NAME_CHARS | frozenset(":")):
                return False
            if literal[:1].lower() == "v":
                version, separator, address = literal[1:].partition(".")
                if (
                    not separator
                    or not version
                    or not set(version) <= _HEX
                    or not address
                    or not all(char in _REG_NAME_CHARS or char == ":" for char in address)
                ):
                    return False
            else:
                address, zone_separator, zone = literal.partition("%25")
                if zone_separator:
                    if (
                        not zone
                        or not zone.isascii()
                        or not _uri_component_is_syntactic(zone, _UNRESERVED)
                    ):
                        return False
                elif "%" in literal:
                    return False
                ipaddress.IPv6Address(address)
            port_suffix = authority[closing + 1 :]
            if port_suffix and not port_suffix.startswith(":"):
                return False
        else:
            host, separator, port = authority.partition(":")
            if not host or not _uri_component_is_syntactic(host, _REG_NAME_CHARS):
                return False
            if separator and ":" in port:
                return False
            port_suffix = separator + port
        if port_suffix and (len(port_suffix) == 1 or not port_suffix[1:].isdigit()):
            return False
        if parsed.hostname is None:
            return False
        _ = parsed.port  # urlsplit defers port range validation.
        return (
            _uri_component_is_syntactic(parsed.path, _PCHAR | frozenset("/"))
            and _uri_component_is_syntactic(parsed.query, _PCHAR | frozenset("/?"))
            and _uri_component_is_syntactic(parsed.fragment, _PCHAR | frozenset("/?"))
        )
    except (TypeError, ValueError):
        return False


def extract_cited_urls(text: str) -> tuple[str, ...]:
    """Extract cited URLs from the fenced evidence JSON block(s) in ``text``.

    Reads the deep-tier contract shape written by
    ``build_lateral_multi_subagent``: ``external_sources`` (list of URLs) and
    ``claims[].source``. Order-preserving, deduplicated. Any malformed block
    is skipped rather than raised — a persona that broke the format simply
    contributes no checkable citations. Preserve nonblank URL strings exactly so
    the prefetch validator can detect padding and other malformed characters.
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
            url = candidate
            if url.strip() and url not in seen:
                seen.add(url)
                urls.append(url)
    return tuple(urls)


def _default_fetch(url: str, timeout: float) -> bool:
    """One bounded liveness probe. True iff the fetch completed with 2xx/3xx.

    HEAD first (cheapest); a server that rejects HEAD outright (405/501) gets
    one ranged GET so it is not falsely marked dead. Anything else — DNS
    failure, TLS failure, timeout, 4xx/5xx — is "not alive right now".
    """
    # urllib's Request rejects percent escapes inside IPv6 ZoneIDs. Decode
    # characters it can represent in the authority without changing delimiters.
    request_url = url
    authority_match = re.match(r"(?i)^https?://([^/?#]*)", url)
    if authority_match:
        authority = authority_match.group(1)
        opening = authority.find("[")
        closing = authority.find("]", opening + 1)
        if opening >= 0 and closing > opening:
            literal = authority[opening + 1 : closing]
            address, separator, zone = literal.partition("%25")
            if separator and "%" in zone:
                zone = re.sub(
                    r"%([0-9A-Fa-f]{2})",
                    lambda match: (
                        char
                        if (char := chr(int(match.group(1), 16)))
                        in (_UNRESERVED | _SUB_DELIMS | frozenset(":"))
                        else match.group(0)
                    ),
                    zone,
                )
                offset = authority_match.start(1)
                request_url = (
                    url[: offset + opening + 1]
                    + address
                    + separator
                    + zone
                    + url[offset + closing :]
                )
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        request = urllib.request.Request(  # noqa: S310 - scheme validated by caller
            request_url,
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
    ``invalid`` (malformed, non-http(s), or oversized URL — never fetched) / ``unchecked``
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
        if not _eligible_http_citation(url):
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
