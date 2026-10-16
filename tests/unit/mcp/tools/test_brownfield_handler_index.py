"""BrownfieldHandler query: registry listing without rescans, and ranked ``text`` search."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ouroboros.mcp.tools import brownfield_handler as handler_module
from ouroboros.mcp.tools.brownfield_handler import BrownfieldHandler
from ouroboros.persistence.brownfield import BrownfieldStore


@pytest.fixture
async def store(tmp_path):
    s = BrownfieldStore(f"sqlite+aiosqlite:///{tmp_path / 'bf.db'}")
    await s.initialize()
    await s.register("/w/web-console", "web-console", desc="React admin dashboard")
    await s.register("/w/billing", "billing", is_default=True)
    await s.register("/w/infra", "infra")
    await s.upsert_index_entries(
        {
            "/w/billing": ("stripe invoices webhooks subscription", "fp"),
            "/w/infra": ("terraform vpc modules", "fp"),
        }
    )
    yield s
    await s.close()


def _no_scan():
    return patch.object(
        handler_module,
        "scan_and_register",
        side_effect=AssertionError("query must not scan"),
    )


async def test_query_lists_registry_numbered_without_scanning(store) -> None:
    handler = BrownfieldHandler(_store=store)
    with _no_scan():
        result = await handler.handle({"action": "query"})

    assert result.is_ok
    text = result.value.text_content
    assert " 1. web-console" in text
    assert " 2. billing *" in text
    assert " 3. infra" in text
    assert "Defaults (* marked): 2 (billing)" in text
    assert result.value.meta["total"] == 3


async def test_query_text_ranks_from_stored_documents(store) -> None:
    handler = BrownfieldHandler(_store=store)
    with _no_scan():
        result = await handler.handle({"action": "query", "text": "stripe subscription"})

    assert result.is_ok
    meta = result.value.meta
    assert [r["name"] for r in meta["repos"]] == ["billing"]
    assert meta["repos"][0]["score"] > 0
    assert meta["matched"] == 1 and meta["total"] == 3
    assert meta["unindexed"] == 1  # web-console has no stored document
    text = result.value.text_content
    assert " 2. billing *  (score" in text
    assert "1 repo(s) have no stored search document" in text


async def test_query_text_without_action_is_detected_as_query(store) -> None:
    handler = BrownfieldHandler(_store=store)
    result = await handler.handle({"text": "react dashboard"})
    assert [r["name"] for r in result.value.meta["repos"]] == ["web-console"]


async def test_query_text_pages_ranked_results(store) -> None:
    await store.upsert_index_entries({"/w/web-console": ("shared", "fp")})
    await store.upsert_index_entries({"/w/billing": ("shared", "fp")})
    await store.upsert_index_entries({"/w/infra": ("shared", "fp")})
    handler = BrownfieldHandler(_store=store)

    full = await handler.handle({"action": "query", "text": "shared"})
    names = [r["name"] for r in full.value.meta["repos"]]
    assert names == ["billing", "infra", "web-console"]

    page = await handler.handle({"action": "query", "text": "shared", "offset": 1, "limit": 1})
    assert [r["name"] for r in page.value.meta["repos"]] == ["infra"]
    assert page.value.meta["matched"] == 3


async def test_query_text_no_match(store) -> None:
    handler = BrownfieldHandler(_store=store)
    result = await handler.handle({"action": "query", "text": "kubernetes"})
    assert result.is_ok
    assert result.value.meta["repos"] == []
    assert "No registered repository matches 'kubernetes'" in result.value.text_content


async def test_query_text_defaults_to_top_ten(tmp_path) -> None:
    s = BrownfieldStore(f"sqlite+aiosqlite:///{tmp_path / 'many.db'}")
    await s.initialize()
    try:
        for i in range(15):
            await s.register(f"/w/svc-{i:02d}", f"svc-{i:02d}")
        handler = BrownfieldHandler(_store=s)
        result = await handler.handle({"action": "query", "text": "svc"})
        meta = result.value.meta
        assert meta["matched"] == 15
        assert meta["limit"] == 10
        assert [r["name"] for r in meta["repos"]] == [f"svc-{i:02d}" for i in range(10)]
    finally:
        await s.close()
