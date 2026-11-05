"""Deterministic citation liveness audit for deep-tier lateral evidence.

Grounded-lateral RFC D4: a hallucinated citation under "recommendation
grounds" is the feature's biggest trust risk — one dead link and the advisory
is worse than no advisory. This module audits the URLs personas cite in their
fenced evidence blocks and reports, per URL, whether it was reachable.

Enforcement is withhold-only, mirroring the verify-gate invariant: an
unreachable or malformed citation is *marked*, never a failure of the tool
call, and a submission with no evidence block touches no network at all.
Socket operations use a timeout and the audit stops starting requests after its
budget. These are not a strict wall-clock bound: DNS and slow responses may
still take longer than the budget.

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
import urllib.request

VERIFIED = "verified"
UNREACHABLE = "unreachable"
INVALID = "invalid"
UNCHECKED = "unchecked"

_REQUEST_TIMEOUT_SECONDS = 4.0
_TOTAL_BUDGET_SECONDS = 10.0
_MAX_URLS = 8
_MAX_URL_LEN = 2000
_MAX_REDIRECTS = 3

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_SUB_DELIMS = frozenset("!$&'()*+,;=")
_REG_NAME_CHARS = _UNRESERVED | _SUB_DELIMS
_PCHAR = _REG_NAME_CHARS | frozenset(":@")
_HEX = frozenset(string.hexdigits)


def _iri_codepoint_allowed(codepoint: int, *, query: bool) -> bool:
    # RFC 3987 ucschar; private-use codepoints are permitted only in iquery.
    ucschar = (
        0xA0 <= codepoint <= 0xD7FF
        or 0xF900 <= codepoint <= 0xFDCF
        or 0xFDF0 <= codepoint <= 0xFFEF
        or (
            0x10000 <= codepoint <= 0xEFFFD
            and codepoint & 0xFFFF <= 0xFFFD
            and not 0xE0000 <= codepoint <= 0xE0FFF
        )
    )
    private = (
        0xE000 <= codepoint <= 0xF8FF
        or 0xF0000 <= codepoint <= 0xFFFFD
        or 0x100000 <= codepoint <= 0x10FFFD
    )
    return ucschar or (query and private)


def _uri_component_is_syntactic(
    value: str, allowed: frozenset[str], *, query: bool = False
) -> bool:
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            if index + 2 >= len(value) or not all(
                digit in _HEX for digit in value[index + 1 : index + 3]
            ):
                return False
            index += 3
        elif char in allowed or _iri_codepoint_allowed(ord(char), query=query):
            index += 1
        else:
            return False
    return True


def _eligible_http_citation(url: str) -> bool:
    if len(url) > _MAX_URL_LEN or any(
        char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in url
    ):
        return False
    # Split raw components before validating: urlsplit rejects valid IPvFuture V.
    parsed = re.fullmatch(r"(?i)https?://([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#(.*))?", url)
    if parsed is None:
        return False
    authority, path, query, fragment = parsed.groups(default="")
    if "@" in authority:
        if authority.count("@") != 1:
            return False
        userinfo, authority = authority.split("@", 1)
        if not _uri_component_is_syntactic(userinfo, _REG_NAME_CHARS | frozenset(":")):
            return False
    if authority.startswith("["):
        literal, closing, port_suffix = authority[1:].partition("]")
        if not closing:
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
            if "%" in address:
                return False
            try:
                ipaddress.IPv6Address(address)
            except ipaddress.AddressValueError:
                return False
        if port_suffix and not port_suffix.startswith(":"):
            return False
    else:
        host, separator, port = authority.partition(":")
        if not host or not _uri_component_is_syntactic(host, _REG_NAME_CHARS):
            return False
        port_suffix = separator + port
    if port_suffix:
        port = port_suffix[1:]
        if not port or not port.isascii() or not port.isdigit() or int(port) > 65535:
            return False
    return (
        _uri_component_is_syntactic(path, _PCHAR | frozenset("/"))
        and _uri_component_is_syntactic(query, _PCHAR | frozenset("/?"), query=True)
        and _uri_component_is_syntactic(fragment, _PCHAR | frozenset("/?"))
    )


def _extract_cited_urls(text: str, *, include_bare: bool) -> tuple[str, ...]:
    """Extract cited URLs from fenced blocks and, optionally, a bare object.

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
    blocks = [match.group(1) for match in _FENCED_JSON_RE.finditer(text)]
    # The shared evidence parser accepts a bare object followed by prose.
    # Only consider an object at the start; do not search arbitrary prose.
    if include_bare and text.lstrip().startswith("{"):
        candidate = text.lstrip()
        try:
            _, end = json.JSONDecoder().raw_decode(candidate)
        except (ValueError, RecursionError):
            pass
        else:
            blocks.append(candidate[:end])
    for block in blocks:
        try:
            payload = json.loads(block)
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


def extract_cited_urls(text: str) -> tuple[str, ...]:
    """Return citations found in fenced blocks or a bare evidence object."""
    return _extract_cited_urls(text, include_bare=True)


class _NoRedirectBody:
    """Allow urllib to close a redirect response without draining its body."""

    def __init__(self, response):
        self._response = response

    def read(self, *args, **kwargs):
        # Citation liveness needs only the redirect status and Location header.
        return b""

    def __getattr__(self, name):
        return getattr(self._response, name)


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Count all redirects, including repeated URLs, per fetch request."""

    def __init__(self, deadline: float = float("inf")):
        super().__init__()
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirects = getattr(req, "_citation_redirects", 0)
        if redirects >= _MAX_REDIRECTS:
            raise urllib.error.HTTPError(req.full_url, code, "redirect limit exceeded", headers, fp)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise urllib.error.HTTPError(
                req.full_url, code, "citation timeout expired", headers, fp
            )
        # urllib follows redirects using the previous request's timeout.
        if self.deadline != float("inf"):
            req.timeout = min(req.timeout, remaining)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected._citation_redirects = redirects + 1
        return redirected

    def http_error_302(self, req, fp, code, msg, headers):
        # stdlib's redirect handler otherwise performs an unbounded fp.read()
        # before opening the next URL. No response body is needed for liveness.
        return super().http_error_302(req, _NoRedirectBody(fp), code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _default_fetch(url: str, timeout: float) -> bool:
    """One bounded liveness probe; accept a response only after opening it.

    HEAD first (cheapest); a server that rejects HEAD outright (405/501) gets
    one ranged GET so it is not falsely marked dead. Anything else — DNS
    failure, TLS failure, timeout or HTTP error is "not alive right now".
    Redirects are limited to three hops per request.
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
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(_BoundedRedirectHandler(deadline))
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        request = urllib.request.Request(  # noqa: S310 - scheme validated by caller
            request_url,
            method=method,
            headers={"User-Agent": "ouroboros-citation-check/1", **headers},
        )
        try:
            with opener.open(request, timeout=remaining):  # noqa: S310
                return time.monotonic() <= deadline
        except urllib.error.HTTPError as error:
            try:
                if method == "HEAD" and error.code in (405, 501):
                    continue  # server refuses HEAD; try the ranged GET once
                # A 3xx HTTPError can indicate the redirect cap was exceeded;
                # it is not evidence of a reachable final source.
                return False
            finally:
                error.close()
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
    all_urls: list[str] = []
    all_seen: set[str] = set()
    fenced_order: list[str] = []
    fenced_urls: set[str] = set()
    for text in texts:
        text = text if isinstance(text, str) else ""
        for url in _extract_cited_urls(text, include_bare=False):
            if url not in fenced_urls:
                fenced_urls.add(url)
                fenced_order.append(url)
        for url in extract_cited_urls(text):
            if url not in all_seen:
                all_seen.add(url)
                all_urls.append(url)
    # Preserve the pre-existing fenced-only fetch order and budget exactly.
    # A bare occurrence must not promote a later fenced URL into the cap.
    urls = fenced_order + [url for url in all_urls if url not in fenced_urls]
    if not urls:
        return None

    verdicts: dict[str, str] = {}
    deadline = time.monotonic() + total_budget_seconds
    checked = 0
    for url in urls:
        if not _eligible_http_citation(url):
            verdicts[url] = INVALID
            continue
        # Bare evidence was not historically a network-fetch input. Surface
        # its citations as unverified without expanding destination access.
        if url not in fenced_urls:
            verdicts[url] = UNCHECKED
            continue
        remaining = min(_REQUEST_TIMEOUT_SECONDS, deadline - time.monotonic())
        if checked >= max_urls or remaining <= 0:
            verdicts[url] = UNCHECKED
            continue
        try:
            alive = bool(fetch(url, remaining))
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
