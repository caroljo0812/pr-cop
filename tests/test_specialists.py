"""Tests for the specialist registry and finding parsing."""
from prcop.specialists import (
    SQUAD,
    Finding,
    SEVERITIES,
    SEVERITY_RANK,
    get_specialist,
    render_user_prompt,
)


def test_squad_has_four_specialists_with_unique_names():
    names = [s.name for s in SQUAD]
    assert sorted(names) == ["performance", "security", "style", "test_coverage"]


def test_severity_ordering_is_worst_first():
    # SEVERITY_RANK is "lower number = worse", so critical=0
    assert SEVERITY_RANK["critical"] == 0
    assert SEVERITY_RANK["info"] == len(SEVERITIES) - 1


def test_get_specialist_lookup():
    assert get_specialist("security").name == "security"
    assert get_specialist("SECURITY").name == "security"
    assert get_specialist("nope") is None


def test_finding_from_dict_normalizes_unknown_severity():
    f = Finding.from_dict({"severity": "WAT", "file": "a", "title": "t"})
    assert f.severity == "info"


def test_finding_from_dict_clamps_title_length():
    long_title = "x" * 500
    f = Finding.from_dict({"severity": "low", "file": "a", "title": long_title})
    assert len(f.title) <= 140


def test_render_user_prompt_includes_meta_and_diff():
    bundle = "FILE foo.py\n@@ +1  print('hi')"
    out = render_user_prompt(
        bundle,
        repo_meta={"repo": "o/r", "base": "main", "head": "feat", "title": "T", "description": "D"},
    )
    assert "Repository: o/r" in out
    assert "Comparing: main..feat" in out
    assert "PR title: T" in out
    assert "FILE foo.py" in out
