"""Page rendering — live SSE page vs frozen static snapshot."""

from __future__ import annotations

from ouroboros.dashboard_web.page import INDEX_HTML, static_html

_BOARD = {
    "meta": {"provider": "hermes_cli", "completed": 1, "total": 4, "phase": "Deliver"},
    "columns": {
        "pending": [],
        "executing": [
            {"id": "n2", "title": "green.txt", "status": "executing", "provider": "hermes_cli"}
        ],
        "completed": [
            {"id": "n1", "title": "red.txt", "status": "completed", "provider": "hermes_cli"}
        ],
        "failed": [],
    },
    "providers": ["hermes_cli"],
}


class TestLivePage:
    def test_index_html_uses_sse(self) -> None:
        assert "EventSource" in INDEX_HTML
        assert "/events?run=" in INDEX_HTML

    def test_index_html_renders_frugality_telemetry(self) -> None:
        # The page must carry the client-side logic for the three telemetry axes.
        assert "TIER_SYMBOL" in INDEX_HTML
        assert "fmtTokens" in INDEX_HTML
        assert "m-tokens" in INDEX_HTML
        assert "m-frugality" in INDEX_HTML
        assert "c.model_tier" in INDEX_HTML
        assert "Frugality:" in INDEX_HTML

    def test_index_html_polls_for_pending_run(self) -> None:
        # The daemon base URL is published before a run exists (auto flow links it
        # while interview/seed are still running). The page must NOT dead-end on the
        # first empty /api/runs — it shows a waiting state and keeps polling.
        assert "waiting for run" in INDEX_HTML
        assert "no active run" not in INDEX_HTML
        # The poll must be a real loop that re-checks /api/runs, not a one-shot.
        assert "while (!runId)" in INDEX_HTML
        assert "pickRun()" in INDEX_HTML
        # And it must be interval-gated so an idle page never spins hot.
        assert "setTimeout" in INDEX_HTML
        assert "WAIT_POLL_MS" in INDEX_HTML


class TestStaticSnapshot:
    def test_static_html_is_self_contained_and_sse_free(self) -> None:
        html = static_html(_BOARD, run_id="exec_1")
        # Renders inline, no live stream — settles immediately (capturable).
        assert "EventSource" not in html
        assert "render(" in html
        assert "snapshot" in html
        # The inlined board data is present.
        assert "hermes_cli" in html
        assert "red.txt" in html and "green.txt" in html

    def test_script_tag_cannot_be_broken_by_board_content(self) -> None:
        # A title containing ``</script>`` must not terminate the inlined script.
        html = static_html(
            {
                "meta": {},
                "columns": {
                    "pending": [{"id": "x", "title": "a </script> b", "status": "pending"}],
                    "executing": [],
                    "completed": [],
                    "failed": [],
                },
                "providers": [],
            },
            run_id="exec_x",
        )
        assert "</script> b" not in html
        assert "<\\/script>" in html

    def test_snapshot_run_id_cannot_break_out_of_script(self) -> None:
        # The ?run= label is caller-controlled; quotes, backslashes and </script>
        # must never escape the inline JS string / innerHTML sink.
        payloads = [
            "x';alert(1);//",
            'x";alert(1);//',
            "x\\';alert(1);//",
            "x</script><script>alert(1)</script>",
        ]
        for run_id in payloads:
            html = static_html(_BOARD, run_id=run_id)
            # The raw payload must not survive into the page verbatim.
            assert run_id not in html
            # The template's own closing tag stays the ONLY </script>.
            assert html.count("</script>") == 1
            assert "<script>alert(1)" not in html
            # The page still bootstraps.
            assert "render(" in html

    def test_snapshot_quote_label_is_html_escaped_not_dropped(self) -> None:
        html = static_html(_BOARD, run_id="x';alert(1);//")
        # Single quote is HTML-escaped (innerHTML sink), so the quote cannot
        # terminate the surrounding JS string literal either.
        assert "&#x27;" in html
        assert "';alert(1);//" not in html

    def test_static_snapshot_inlines_telemetry_fields(self) -> None:
        board = {
            "meta": {"total_tokens": 1500.0, "frugality": {"status": "proven", "reason": "ok"}},
            "columns": {
                "pending": [],
                "executing": [
                    {
                        "id": "n1",
                        "title": "AC 1",
                        "status": "executing",
                        "model_tier": "frugal",
                        "tokens": 1500.0,
                    }
                ],
                "completed": [],
                "failed": [],
            },
            "providers": [],
        }
        html = static_html(board, run_id="exec_1")
        # The telemetry values survive into the inlined board JSON.
        assert "model_tier" in html and "frugal" in html
        assert "1500" in html
        assert "proven" in html
