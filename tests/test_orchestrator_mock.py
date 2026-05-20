"""Smoke tests using PRCOP_LLM_PROVIDER=mock so we don't hit the network."""
import asyncio

import pytest

from prcop.orchestrator import (
    render_markdown_report,
    render_text_report,
    review_diff,
)

SMALL_DIFF = """diff --git a/app/util.py b/app/util.py
index 1111111..2222222 100644
--- a/app/util.py
+++ b/app/util.py
@@ -1,3 +1,5 @@
 def greet(name):
-    return "hi " + name
+    # Prone to None concatenation if name is None
+    return "hi " + str(name)
+    PASSWORD = "hunter2"
"""


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    monkeypatch.setenv("PRCOP_LLM_PROVIDER", "mock")
    monkeypatch.delenv("PRCOP_LLM_API_KEY", raising=False)


def test_review_diff_runs_against_mock_provider():
    result = asyncio.run(review_diff(diff_text=SMALL_DIFF, repo_meta={"repo": "x/y"}))
    assert result.file_count == 1
    assert result.duration_ms >= 0
    # Mock provider returns a single info-level finding per specialist; after
    # dedup we expect at most one finding per specialist (4 in total or fewer).
    assert 0 <= len(result.findings) <= 4
    # All four specialists must have run.
    assert {r.specialist for r in result.runs} == {"security", "performance", "style", "test_coverage"}


def test_render_text_report_contains_verdict_header():
    result = asyncio.run(review_diff(diff_text=SMALL_DIFF))
    text = render_text_report(result)
    assert "PR COP REVIEW" in text
    assert "Reviewer:" in text


def test_render_markdown_report_has_table_when_findings_exist():
    result = asyncio.run(review_diff(diff_text=SMALL_DIFF))
    md = render_markdown_report(result)
    assert "## PR Cop review" in md
    if result.findings:
        assert "| sev |" in md


def test_provider_info_falls_back_to_mock_without_key(monkeypatch):
    # Even if user sets provider=mimo but no key, effective provider is mock.
    monkeypatch.setenv("PRCOP_LLM_PROVIDER", "mimo")
    monkeypatch.delenv("PRCOP_LLM_API_KEY", raising=False)
    from prcop.llm import provider_info
    info = provider_info()
    assert info["configured_provider"] == "mimo"
    assert info["effective_provider"] == "mock"
    assert info["default_model"] == "mimo-v2.5-pro"
