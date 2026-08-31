"""Tests for the deep-tier citation liveness audit (grounded-lateral RFC D4).

All tests inject a fake fetcher — the unit suite never touches the network.
"""

from __future__ import annotations

from ouroboros.mcp.tools.citation_check import (
    INVALID,
    UNCHECKED,
    UNREACHABLE,
    VERIFIED,
    audit_citations,
    extract_cited_urls,
)

_EVIDENCE = (
    "My analysis...\n"
    "```json\n"
    '{"external_sources": ["https://a.example/doc", "https://b.example/spec"],\n'
    ' "claims": [{"claim": "X supports Y", "source": "https://a.example/doc"},\n'
    '            {"claim": "Z is default", "source": "https://c.example/faq"}]}\n'
    "```\n"
)


def test_extracts_urls_from_evidence_block_deduplicated_in_order() -> None:
    assert extract_cited_urls(_EVIDENCE) == (
        "https://a.example/doc",
        "https://b.example/spec",
        "https://c.example/faq",
    )


def test_malformed_block_and_plain_text_yield_nothing() -> None:
    assert extract_cited_urls("no evidence here") == ()
    assert extract_cited_urls("```json\n{not json]\n```") == ()


def test_no_citations_means_no_audit_and_no_network() -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bool:
        calls.append(url)
        return True

    assert audit_citations(["plain opinion output"], fetch=fetch) is None
    assert calls == []


def test_dead_citation_is_marked_not_raised() -> None:
    def fetch(url: str, timeout: float) -> bool:
        return url != "https://c.example/faq"

    audit = audit_citations([_EVIDENCE], fetch=fetch)
    assert audit is not None
    assert audit["urls"]["https://a.example/doc"] == VERIFIED
    assert audit["urls"]["https://c.example/faq"] == UNREACHABLE
    assert audit["unverified_present"] is True


def test_non_http_url_is_invalid_and_never_fetched() -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bool:
        calls.append(url)
        return True

    text = '```json\n{"external_sources": ["file:///etc/passwd", "https://ok.example"]}\n```'
    audit = audit_citations([text], fetch=fetch)
    assert audit is not None
    assert audit["urls"]["file:///etc/passwd"] == INVALID
    assert audit["urls"]["https://ok.example"] == VERIFIED
    assert calls == ["https://ok.example"]


def test_url_count_cap_marks_rest_unchecked() -> None:
    urls = [f"https://site{i}.example/page" for i in range(12)]
    text = "```json\n" + '{"external_sources": ' + str(urls).replace("'", '"') + "}\n```"

    audit = audit_citations([text], fetch=lambda url, timeout: True, max_urls=3)
    assert audit is not None
    verdicts = list(audit["urls"].values())
    assert verdicts.count(VERIFIED) == 3
    assert verdicts.count(UNCHECKED) == 9
    assert audit["checked"] == 3


def test_fetcher_exception_degrades_to_unreachable() -> None:
    def fetch(url: str, timeout: float) -> bool:
        raise RuntimeError("dns exploded")

    audit = audit_citations([_EVIDENCE], fetch=fetch)
    assert audit is not None
    assert set(audit["urls"].values()) == {UNREACHABLE}
