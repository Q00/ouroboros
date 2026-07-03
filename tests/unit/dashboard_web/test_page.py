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
