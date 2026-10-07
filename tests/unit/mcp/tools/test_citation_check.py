"""Tests for the deep-tier citation liveness audit (grounded-lateral RFC D4).

All tests inject a fake fetcher — the unit suite never touches the network.
"""

from __future__ import annotations

import json
from unittest.mock import patch

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
    assert audit_citations(['```json\n{"external_sources": ["   "]}\n```'], fetch=fetch) is None
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

    audit = audit_citations([text], fetch=lambda _url, _timeout: True, max_urls=3)
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


def test_malformed_url_is_invalid_and_does_not_raise() -> None:
    audit = audit_citations(
        ['```json\n{"external_sources": ["https://[invalid"]}\n```'],
        fetch=lambda _url, _timeout: True,
    )
    assert audit is not None
    assert audit["urls"]["https://[invalid"] == INVALID


def test_malformed_authorities_are_invalid_without_stopping_later_urls() -> None:
    malformed = (
        "https://example.com:not-a-port",
        "https://example.com:65536",
        "https://example.com:",
        "https://bad host.example/doc",
        "https://bad\nhost.example/doc",
        "https://bad\rhost.example/doc",
        "https://bad\thost.example/doc",
        "https://bad\x80host.example/doc",
        "https://%zz.example/doc",
        "https://ok.example/%zz",
    )
    valid = "https://ok.example/doc"
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    text = "```json\n" + json.dumps({"external_sources": [*malformed, valid]}) + "\n```"
    audit = audit_citations([text], fetch=fetch)

    assert audit is not None
    assert all(audit["urls"][url] == INVALID for url in malformed)
    assert audit["urls"][valid] == VERIFIED
    assert audit["checked"] == 1
    assert calls == [valid]


def test_malformed_urls_do_not_consume_the_valid_url_limit() -> None:
    malformed = [f"https://%z{index}.example/doc" for index in range(8)]
    valid = "https://ok.example/a%20b?q=ok"
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    text = "```json\n" + json.dumps({"external_sources": [*malformed, valid]}) + "\n```"
    audit = audit_citations([text], fetch=fetch, max_urls=8)

    assert audit is not None
    assert all(audit["urls"][url] == INVALID for url in malformed)
    assert audit["urls"][valid] == VERIFIED
    assert audit["checked"] == 1
    assert calls == [valid]


def test_internationalized_ipvfuture_and_encoded_zone_urls_remain_auditable() -> None:
    urls = (
        "https://bücher.example/über",
        "https://[v1.fe80]/doc",
        "https://[fe80::1%25eth0]/doc",
        "https://[fe80::1%25eth%2D0]/doc",
    )
    calls: list[str] = []

    def fetch(candidate: str, _timeout: float) -> bool:
        calls.append(candidate)
        return True

    text = "```json\n" + json.dumps({"external_sources": urls}, ensure_ascii=False) + "\n```"
    audit = audit_citations([text], fetch=fetch)

    assert audit is not None
    assert all(audit["urls"][url] == VERIFIED for url in urls)
    assert calls == list(urls)


def test_raw_malformed_citations_are_rejected_before_the_valid_url_budget() -> None:
    malformed = (
        " https://bad.example/doc ",
        "https://[fe80::1%eth0]/doc",
        "https://[fe80::1%2Feth0]/doc",
        "https://[fe80::1%25]/doc",
    )
    valid = "https://ok.example/doc"
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    text = "```json\n" + json.dumps({"external_sources": [*malformed, valid]}) + "\n```"
    audit = audit_citations([text], fetch=fetch, max_urls=1)

    assert audit is not None
    assert all(audit["urls"][url] == INVALID for url in malformed)
    assert audit["urls"][valid] == VERIFIED
    assert audit["checked"] == 1
    assert calls == [valid]


def test_urllib_representable_encoded_zones_are_fetchable() -> None:
    for zone, normalized in (("eth%2D0", "eth-0"), ("eth%3D0", "eth=0")):
        url = f"https://[fe80::1%25{zone}]/doc"
        text = "```json\n" + json.dumps({"external_sources": [url]}) + "\n```"

        with patch("urllib.request.urlopen") as urlopen:
            audit = audit_citations([text])

        assert audit is not None
        assert audit["urls"][url] == VERIFIED
        urlopen.assert_called_once()
        assert urlopen.call_args.args[0].full_url == f"https://[fe80::1%25{normalized}]/doc"
