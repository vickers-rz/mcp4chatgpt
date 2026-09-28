from pathlib import Path
import runpy

import pytest


benchmark = runpy.run_path(str(Path(__file__).parents[1] / "benchmarks" / "read_only.py"))


def test_pdf_summary_keeps_partial_failures():
    def call(name, arguments):
        if arguments["path"] == "failed.pdf":
            raise ValueError("unavailable")
        return {"page_count": 2, "pages": [{"width": 595, "height": 842}]}
    result = benchmark["pdf_summary"](call, [Path("ok.pdf"), Path("failed.pdf")])
    assert result == {"checked": 2, "abnormal": [], "errors": [{"file": "failed.pdf", "error": "ValueError"}]}


def test_pagination_cycle_fails_instead_of_returning_incomplete_statistics():
    with pytest.raises(ValueError, match="pagination_limit"):
        benchmark["page_summary"](lambda *args: {"rows": [], "next_cursor": 0})


def test_top_n_has_stable_ties_across_pages():
    pages = [
        {"rows": [{"id": 2, "score": 4}, {"id": 3, "score": 1}], "next_cursor": 1},
        {"rows": [{"id": 1, "score": 4}], "next_cursor": None},
    ]
    result = benchmark["page_summary"](lambda name, arguments: pages[arguments["cursor"]])
    assert result == {"count": 3, "score_total": 9, "top5": [
        {"id": 1, "score": 4}, {"id": 2, "score": 4}, {"id": 3, "score": 1}]}
