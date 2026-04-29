"""Tests for BrownfieldHandler — action dispatch, parameter schema, CRUD routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.mcp.tools.brownfield_handler import (
    BrownfieldHandler,
    _detect_action,
)
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore

# ── Helpers ──────────────────────────────────────────────────────


def _make_store_stub(
    repos: list[BrownfieldRepo] | None = None,
    default: BrownfieldRepo | None = None,
    total: int | None = None,
) -> BrownfieldStore:
    """Create a BrownfieldStore mock with configurable data."""
    all_repos = list(repos or [])
    store = AsyncMock(spec=BrownfieldStore)
    store.list = AsyncMock(return_value=all_repos)
    store.count = AsyncMock(return_value=total if total is not None else len(all_repos))
    store.get_default = AsyncMock(return_value=default)
    store.get_defaults = AsyncMock(return_value=[default] if default else [])
    store.register = AsyncMock(
        side_effect=lambda path, name, desc=None, is_default=False: BrownfieldRepo(
            path=path,
            name=name,
            desc=desc,
            is_default=is_default,
        )
    )
    store.set_single_default = AsyncMock(
        side_effect=lambda path: next(
            (
                BrownfieldRepo(path=r.path, name=r.name, desc=r.desc, is_default=True)
                for r in all_repos
                if r.path == path
            ),
            None,
        )
    )
    store.update_is_default = AsyncMock(
        side_effect=lambda path, is_default=True: next(
            (
                BrownfieldRepo(path=r.path, name=r.name, desc=r.desc, is_default=is_default)
                for r in all_repos
                if r.path == path
            ),
            None,
        )
    )
    store.initialize = AsyncMock()
    store.close = AsyncMock()
    return store


_REPO_A = BrownfieldRepo(path="/home/user/repo-a", name="repo-a", desc="Project A", is_default=True)
_REPO_B = BrownfieldRepo(
    path="/home/user/repo-b", name="repo-b", desc="Project B", is_default=False
)


# ── _detect_action tests ─────────────────────────────────────────


class TestDetectAction:
    """Test action auto-detection from parameter presence."""

    def test_explicit_action(self) -> None:
        assert _detect_action({"action": "scan"}) == "scan"
        assert _detect_action({"action": "register"}) == "register"
        assert _detect_action({"action": "query"}) == "query"
        assert _detect_action({"action": "set_default"}) == "set_default"

    def test_path_with_name_implies_register(self) -> None:
        assert _detect_action({"path": "/some/path", "name": "repo"}) == "register"

    def test_path_without_name_implies_register(self) -> None:
        assert _detect_action({"path": "/some/path"}) == "register"

    def test_is_default_implies_set_default(self) -> None:
        assert _detect_action({"is_default": True, "path": "/p"}) == "set_default"
        assert _detect_action({"is_default": False, "path": "/p"}) == "set_default"

    def test_empty_defaults_to_query(self) -> None:
        assert _detect_action({}) == "query"

    def test_explicit_overrides_auto(self) -> None:
        """Explicit action takes precedence over path-based detection."""
        assert _detect_action({"action": "set_default", "path": "/some/path"}) == "set_default"
        assert _detect_action({"action": "register", "path": "/some/path"}) == "register"


# ── Tool definition tests ────────────────────────────────────────


class TestBrownfieldHandlerDefinition:
    """Test the tool definition schema."""

    def test_tool_name(self) -> None:
        handler = BrownfieldHandler()
        defn = handler.definition
        assert defn.name == "ouroboros_brownfield"

    def test_has_action_parameter_with_enum(self) -> None:
        handler = BrownfieldHandler()
        defn = handler.definition
        action_param = next(p for p in defn.parameters if p.name == "action")
        assert action_param.enum == ("scan", "register", "query", "set_default", "set_defaults")
        assert action_param.required is False

    def test_has_all_expected_parameters(self) -> None:
        handler = BrownfieldHandler()
        defn = handler.definition
        param_names = {p.name for p in defn.parameters}
        assert param_names == {
            "action",
            "path",
            "name",
            "desc",
            "is_default",
            "default_only",
            "scan_root",
            "offset",
            "limit",
            "indices",
        }

    def test_input_schema_generation(self) -> None:
        """Verify to_input_schema produces valid JSON Schema."""
        handler = BrownfieldHandler()
        schema = handler.definition.to_input_schema()
        assert schema["type"] == "object"
        assert "action" in schema["properties"]
        assert "enum" in schema["properties"]["action"]
        # No required params — all are optional
        assert schema["required"] == []


# ── Action dispatch tests ─────────────────────────────────────────


class TestBrownfieldHandlerDispatch:
    """Test action routing in the handle method."""

    @pytest.mark.asyncio
    async def test_query_action_returns_repos(self) -> None:
        store = _make_store_stub(repos=[_REPO_A, _REPO_B], default=_REPO_A)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "query"
        assert meta["count"] == 2
        assert len(meta["repos"]) == 2

    @pytest.mark.asyncio
    async def test_query_default_only(self) -> None:
        store = _make_store_stub(repos=[_REPO_A], default=_REPO_A)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "default_only": True})

        assert result.is_ok
        meta = result.value.meta
        assert meta["default_only"] is True
        assert meta["default"]["name"] == "repo-a"

    @pytest.mark.asyncio
    async def test_query_default_only_none(self) -> None:
        store = _make_store_stub(repos=[], default=None)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "default_only": True})

        assert result.is_ok
        assert result.value.meta["default"] is None

    @pytest.mark.asyncio
    async def test_register_action(self) -> None:
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle(
            {
                "action": "register",
                "path": "/home/user/new-repo",
                "name": "new-repo",
                "desc": "A new project",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "register"
        assert meta["repo"]["path"] == "/home/user/new-repo"
        store.register.assert_called_once_with(
            path="/home/user/new-repo",
            name="new-repo",
            desc="A new project",
        )

    @pytest.mark.asyncio
    async def test_register_defaults_name_to_basename(self) -> None:
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle(
            {
                "action": "register",
                "path": "/home/user/my-project",
            }
        )

        assert result.is_ok
        store.register.assert_called_once_with(
            path="/home/user/my-project",
            name="my-project",
            desc=None,
        )

    @pytest.mark.asyncio
    async def test_register_requires_path(self) -> None:
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "register"})

        assert result.is_err
        assert "path" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_set_default_action(self) -> None:
        store = _make_store_stub(repos=[_REPO_A, _REPO_B], default=_REPO_A)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle(
            {
                "action": "set_default",
                "path": "/home/user/repo-b",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "set_default"
        assert meta["repo"]["name"] == "repo-b"

    @pytest.mark.asyncio
    async def test_set_default_requires_path(self) -> None:
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "set_default"})

        assert result.is_err
        assert "path" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_set_default_not_found(self) -> None:
        store = _make_store_stub(repos=[])
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle(
            {
                "action": "set_default",
                "path": "/nonexistent",
            }
        )

        assert result.is_err
        assert "not found" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_unknown_action_returns_error(self) -> None:
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "delete_all"})

        assert result.is_err
        assert "unknown action" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_scan_action(self) -> None:
        store = _make_store_stub(repos=[_REPO_A], default=_REPO_A)
        handler = BrownfieldHandler(_store=store)

        with patch(
            "ouroboros.mcp.tools.brownfield_handler.scan_and_register",
            new_callable=AsyncMock,
            return_value=[_REPO_A],
        ):
            result = await handler.handle({"action": "scan"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "scan"
        assert meta["count"] == 1

    @pytest.mark.asyncio
    async def test_scan_rejects_missing_scan_root(self, tmp_path) -> None:
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)
        missing = tmp_path / "missing"

        with patch(
            "ouroboros.mcp.tools.brownfield_handler.scan_and_register",
            new_callable=AsyncMock,
        ) as scan_mock:
            result = await handler.handle({"action": "scan", "scan_root": str(missing)})

        assert result.is_err
        assert "scan_root must be an existing directory" in str(result.error)
        scan_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scan_rejects_file_scan_root(self, tmp_path) -> None:
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)
        scan_file = tmp_path / "not-a-directory.txt"
        scan_file.write_text("not a directory")

        with patch(
            "ouroboros.mcp.tools.brownfield_handler.scan_and_register",
            new_callable=AsyncMock,
        ) as scan_mock:
            result = await handler.handle({"action": "scan", "scan_root": str(scan_file)})

        assert result.is_err
        assert "scan_root must be an existing directory" in str(result.error)
        scan_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_detect_register(self) -> None:
        """Providing path + name without action auto-detects 'register'."""
        store = _make_store_stub()
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"path": "/home/user/auto-detect", "name": "auto-detect"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "register"

    @pytest.mark.asyncio
    async def test_auto_detect_set_default(self) -> None:
        """Providing path with is_default auto-detects 'set_default'."""
        repo = BrownfieldRepo(path="/home/user/my-repo", name="my-repo")
        store = _make_store_stub(repos=[repo])
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"path": "/home/user/my-repo", "is_default": True})

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "set_default"

    @pytest.mark.asyncio
    async def test_path_only_auto_detects_register(self) -> None:
        """Providing path alone (no is_default) auto-detects 'register'."""
        store = _make_store_stub(repos=[])
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"path": "/home/user/my-repo"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "register"

    @pytest.mark.asyncio
    async def test_auto_detect_query(self) -> None:
        """Empty args auto-detects 'query'."""
        store = _make_store_stub(repos=[], default=None)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({})

        assert result.is_ok
        meta = result.value.meta
        assert meta["action"] == "query"

    @pytest.mark.asyncio
    async def test_query_empty_repos_suggests_scan(self) -> None:
        store = _make_store_stub(repos=[], default=None)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query"})

        assert result.is_ok
        assert "scan" in result.value.text_content.lower()

    @pytest.mark.asyncio
    async def test_injected_store_initializes_once(self) -> None:
        store = _make_store_stub(repos=[_REPO_A], default=_REPO_A)
        handler = BrownfieldHandler(_store=store)

        await handler.handle({"action": "query"})
        await handler.handle({"action": "query"})

        store.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_shared_init_recovers_on_next_request(self) -> None:
        """A failed shared-store init must not wedge the handler.

        Regression for the readiness contract: ``_store_ready`` may only flip to
        ``True`` after ``initialize()`` returns successfully, so the next
        request retries instead of inheriting a half-initialized store.
        """
        store = _make_store_stub(repos=[_REPO_A], default=_REPO_A)
        store.initialize.side_effect = [RuntimeError("transient init failure"), None]
        handler = BrownfieldHandler(_store=store)

        first = await handler.handle({"action": "query"})
        assert not first.is_ok
        assert "transient init failure" in str(first.error)

        second = await handler.handle({"action": "query"})
        assert second.is_ok
        assert store.initialize.await_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_first_requests_initialize_once(self) -> None:
        """Concurrent first requests must serialize initialization.

        Without ``_init_lock``, two coroutines hitting ``_get_store()`` at the
        same time on a not-yet-ready injected store could each call
        ``initialize()``. With the lock, the second coroutine waits and sees
        ``_store_ready`` already True after the first finishes.
        """
        import asyncio

        store = _make_store_stub(repos=[_REPO_A], default=_REPO_A)
        init_started = asyncio.Event()
        release_init = asyncio.Event()

        async def slow_initialize() -> None:
            init_started.set()
            await release_init.wait()

        store.initialize.side_effect = slow_initialize
        handler = BrownfieldHandler(_store=store)

        first = asyncio.create_task(handler.handle({"action": "query"}))
        await init_started.wait()
        second = asyncio.create_task(handler.handle({"action": "query"}))
        # Give the second task a chance to enter _get_store and block on the
        # lock without racing past the first's still-in-progress initialize.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release_init.set()

        first_result, second_result = await asyncio.gather(first, second)

        assert first_result.is_ok
        assert second_result.is_ok
        store.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_lazy_requests_do_not_close_in_use_store(self) -> None:
        """Concurrent requests on a non-injected handler must not close a
        cached store while another in-flight request is still using it.

        Regression for the PR #507 review finding: with the init lock added
        in this PR, two parallel first requests share one lazily-created
        cached store. The pre-fix ``owned_store = self._store is None``
        snapshot let the request that finished first close the store in its
        ``finally`` while the slower one was still mid-query, surfacing as
        closed-connection / ``PersistenceError`` failures under parallel
        brownfield tool calls.

        Construction:
        - ``initialize()`` is slow so the second request enters ``handle()``
          while the first still holds the init lock; both therefore see the
          same cached store after init unlocks.
        - The first ``count()`` call returns immediately (the first request
          finishes its dispatch quickly); the second blocks, leaving that
          request in-flight when the first reaches its ``finally``.
        - The test then asserts ``close`` was *not* awaited yet. With the
          refcount-tracked ownership the first request observes
          ``refcount > 0`` and skips the close; with the buggy snapshot it
          would already have closed the still-shared store.
        """
        import asyncio

        init_started = asyncio.Event()
        release_init = asyncio.Event()
        second_count_started = asyncio.Event()
        release_second_count = asyncio.Event()

        store_stub = _make_store_stub(repos=[_REPO_A], default=_REPO_A)

        async def slow_initialize() -> None:
            init_started.set()
            await release_init.wait()

        store_stub.initialize = AsyncMock(side_effect=slow_initialize)

        count_calls: list[str] = []

        async def conditionally_slow_count() -> int:
            count_calls.append("call")
            if len(count_calls) == 2:
                second_count_started.set()
                await release_second_count.wait()
            return 1

        store_stub.count = AsyncMock(side_effect=conditionally_slow_count)

        with patch(
            "ouroboros.mcp.tools.brownfield_handler.BrownfieldStore",
            return_value=store_stub,
        ) as MockStore:
            handler = BrownfieldHandler()  # no injected store — lazy path

            first = asyncio.create_task(handler.handle({"action": "query"}))
            await init_started.wait()
            # First is mid-init holding the init lock. Start the second so
            # it snapshots state and blocks waiting for the same lock —
            # this is the interleaving the OLD ownership snapshot mishandled.
            second = asyncio.create_task(handler.handle({"action": "query"}))
            for _ in range(5):
                await asyncio.sleep(0)

            release_init.set()
            await second_count_started.wait()

            # The first request has reached its ``finally`` by now (its
            # ``count`` returned immediately); the second is still mid-query.
            # The store must NOT be closed yet — closing here is the exact
            # close-while-in-use bug the refcount fix prevents.
            store_stub.close.assert_not_awaited()

            release_second_count.set()
            first_result, second_result = await asyncio.gather(first, second)

        assert first_result.is_ok
        assert second_result.is_ok
        # Lazy store created and initialized exactly once, closed exactly
        # once after every concurrent request has finished.
        assert MockStore.call_count == 1
        store_stub.initialize.assert_awaited_once()
        store_stub.close.assert_awaited_once()


# ── Pagination tests ──────────────────────────────────────────────


_REPO_C = BrownfieldRepo(
    path="/home/user/repo-c", name="repo-c", desc="Project C", is_default=False
)
_REPO_D = BrownfieldRepo(
    path="/home/user/repo-d", name="repo-d", desc="Project D", is_default=False
)


class TestBrownfieldHandlerPagination:
    """Test offset/limit pagination for the query action."""

    @pytest.mark.asyncio
    async def test_query_with_limit(self) -> None:
        """Passing limit returns only that many repos."""
        store = _make_store_stub(repos=[_REPO_A, _REPO_B], default=_REPO_A, total=4)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "limit": 2})

        assert result.is_ok
        meta = result.value.meta
        assert meta["total"] == 4
        assert meta["count"] == 2
        assert meta["offset"] == 0
        assert meta["limit"] == 2
        store.list.assert_called_once_with(offset=0, limit=2)

    @pytest.mark.asyncio
    async def test_query_with_offset(self) -> None:
        """Passing offset skips leading rows."""
        store = _make_store_stub(repos=[_REPO_C, _REPO_D], default=None, total=4)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "offset": 2})

        assert result.is_ok
        meta = result.value.meta
        assert meta["total"] == 4
        assert meta["count"] == 2
        assert meta["offset"] == 2
        assert meta["limit"] is None
        store.list.assert_called_once_with(offset=2, limit=None)

    @pytest.mark.asyncio
    async def test_query_with_offset_and_limit(self) -> None:
        """Combined offset + limit for page-style navigation."""
        store = _make_store_stub(repos=[_REPO_B], default=None, total=4)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "offset": 1, "limit": 1})

        assert result.is_ok
        meta = result.value.meta
        assert meta["total"] == 4
        assert meta["count"] == 1
        assert meta["offset"] == 1
        assert meta["limit"] == 1
        assert len(meta["repos"]) == 1
        store.list.assert_called_once_with(offset=1, limit=1)

    @pytest.mark.asyncio
    async def test_query_defaults_no_pagination(self) -> None:
        """Without offset/limit, returns all repos with pagination metadata."""
        store = _make_store_stub(repos=[_REPO_A, _REPO_B], default=_REPO_A)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query"})

        assert result.is_ok
        meta = result.value.meta
        assert meta["total"] == 2
        assert meta["count"] == 2
        assert meta["offset"] == 0
        assert meta["limit"] is None
        store.list.assert_called_once_with(offset=0, limit=None)

    @pytest.mark.asyncio
    async def test_query_offset_beyond_total_returns_empty_page(self) -> None:
        """Offset past all rows returns an empty page (but total is still correct)."""
        store = _make_store_stub(repos=[], default=None, total=2)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "offset": 10})

        assert result.is_ok
        meta = result.value.meta
        assert meta["total"] == 2
        assert meta["count"] == 0
        assert meta["offset"] == 10
        assert meta["repos"] == []

    @pytest.mark.asyncio
    async def test_query_pagination_meta_in_empty_db(self) -> None:
        """Empty DB still includes pagination metadata."""
        store = _make_store_stub(repos=[], default=None, total=0)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "offset": 0, "limit": 10})

        assert result.is_ok
        meta = result.value.meta
        assert meta["total"] == 0
        assert meta["count"] == 0
        assert meta["offset"] == 0
        assert meta["limit"] == 10

    @pytest.mark.asyncio
    async def test_query_text_includes_total(self) -> None:
        """The text content shows total and page size."""
        store = _make_store_stub(repos=[_REPO_A], default=_REPO_A, total=3)
        handler = BrownfieldHandler(_store=store)

        result = await handler.handle({"action": "query", "offset": 0, "limit": 1})

        assert result.is_ok
        text = result.value.text_content
        assert "3 total" in text
        assert "showing 1" in text
