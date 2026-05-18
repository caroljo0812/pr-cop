"""Tests for the consensus pass — dedup, severity merging, sorting."""
import asyncio

from prcop.consensus import deduplicate, sort_findings, write_verdict
from prcop.specialists import Finding


def make(specialist, file, line, severity, category, title, rationale="r"):
    return Finding(
        specialist=specialist, file=file, line=line, severity=severity,
        category=category, title=title, rationale=rationale,
    )


def test_dedup_merges_same_file_line_category():
    findings = [
        make("security", "a.py", 10, "high", "injection", "SQL injection in query"),
        make("style", "a.py", 10, "low", "injection", "Looks like SQL injection"),
    ]
    merged = deduplicate(findings)
    assert len(merged) == 1
    m = merged[0]
    assert "security" in m.specialist and "style" in m.specialist
    # Two specialists agreed → severity bumps one rung from high → critical
    assert m.severity == "critical"


def test_dedup_keeps_distinct_categories_separate():
    findings = [
        make("security", "a.py", 10, "high", "injection", "SQL"),
        make("security", "a.py", 10, "medium", "perf", "N+1"),
    ]
    merged = deduplicate(findings)
    assert len(merged) == 2


def test_dedup_picks_longer_rationale():
    findings = [
        make("a", "x.py", 5, "low", "style", "rename", rationale="short"),
        make("b", "x.py", 5, "low", "style", "rename", rationale="this is a much longer reason"),
    ]
    merged = deduplicate(findings)
    assert merged[0].rationale == "this is a much longer reason"


def test_sort_findings_orders_by_severity_then_file():
    findings = [
        make("a", "z.py", 1, "low", "x", "low"),
        make("b", "a.py", 2, "critical", "x", "crit"),
        make("c", "m.py", 3, "high", "x", "high"),
    ]
    out = sort_findings(findings)
    sevs = [f.severity for f in out]
    assert sevs == ["critical", "high", "low"]


def test_write_verdict_returns_static_when_empty():
    text = asyncio.run(write_verdict([], file_count=0))
    assert "Approve" in text
