"""Specialist reviewer agents.

Each specialist is a (system_prompt, name, focus) tuple. The orchestrator runs
them in parallel against the same diff bundle and collects findings.

We deliberately keep the specialist contract small:
- Input: a rendered diff bundle (path, line numbers, hunks).
- Output: JSON object {"findings": [Finding, ...]} where each Finding is a
  strict shape the orchestrator can union and the consensus pass can score.

Severity scale (intentionally small, easy for LLMs to be consistent on):
  critical | high | medium | low | info
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

# Severity is ordered from worst to least severe so we can sort.
SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    specialist: str
    file: str
    line: int | None
    severity: str
    category: str
    title: str
    rationale: str
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "specialist": self.specialist,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "rationale": self.rationale,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], default_specialist: str = "unknown") -> "Finding":
        sev = str(d.get("severity") or "info").lower()
        if sev not in SEVERITY_RANK:
            sev = "info"
        line_raw = d.get("line")
        try:
            line = int(line_raw) if line_raw is not None else None
        except (TypeError, ValueError):
            line = None
        return cls(
            specialist=str(d.get("specialist") or default_specialist),
            file=str(d.get("file") or "<unknown>"),
            line=line,
            severity=sev,
            category=str(d.get("category") or "general"),
            title=str(d.get("title") or "").strip()[:140] or "Untitled finding",
            rationale=str(d.get("rationale") or "").strip(),
            suggestion=(str(d["suggestion"]).strip() if d.get("suggestion") else None),
        )


@dataclass
class Specialist:
    name: str
    focus: str
    system_prompt: str
    style_hint: str = ""
    # Hard cap on findings the specialist may emit. Keeps signal-to-noise high.
    max_findings: int = 6


_BASE_OUTPUT_CONTRACT = """
You MUST return a single JSON object with this exact shape:

{
  "findings": [
    {
      "file": "<path/exactly/as/in/diff>",
      "line": <integer line number in the NEW file, or null if file-level>,
      "severity": "critical|high|medium|low|info",
      "category": "<short tag, e.g. injection, race, n+1, hardcoded-secret, missing-test>",
      "title": "<<= 100 chars, imperative, concrete>",
      "rationale": "<<= 320 chars, explain the problem with reference to specific changed lines>",
      "suggestion": "<optional, <= 240 chars, code-level fix idea>"
    }
  ]
}

Rules:
- Only flag issues introduced or made worse by the diff. Pre-existing problems
  outside the diff are out of scope unless directly relevant.
- Cite real line numbers from the rendered diff (the lines prefixed with "+").
- Do not invent files that are not in the diff bundle.
- If you have nothing to flag, return {"findings": []}. Do not pad with filler.
- Output JSON only. No prose, no markdown fences.
""".strip()


SECURITY = Specialist(
    name="security",
    focus="injection, auth, crypto, secrets, deserialization, SSRF, XSS, path traversal",
    system_prompt=(
        "You are the SECURITY specialist on a code review squad.\n"
        "Your job is to find issues that would let an attacker do something they "
        "shouldn't: injection (SQL, command, prompt), broken auth/session, weak "
        "or misused crypto, hardcoded secrets, unsafe deserialization, SSRF, XSS, "
        "path traversal, missing input validation, insecure defaults, and "
        "regressions in security headers or TLS settings.\n"
        "Be concrete. Quote the offending pattern. If the diff fixes a security "
        "issue, do not flag — only flag what the diff introduces or worsens.\n\n"
        + _BASE_OUTPUT_CONTRACT
    ),
)

PERFORMANCE = Specialist(
    name="performance",
    focus="N+1 queries, blocking I/O on async paths, allocations, hot loops, big-O regressions",
    system_prompt=(
        "You are the PERFORMANCE specialist on a code review squad.\n"
        "Your job is to find changes that will hurt latency, throughput, or "
        "memory: N+1 queries, sync I/O on async paths, unbounded loops, heavy "
        "allocations in hot paths, missing indexes, repeated re-renders, "
        "dropped pagination, and accidental O(n^2) where O(n) was free.\n"
        "Cite the changed lines. Skip micro-optimizations. Skip cosmetic perf "
        "concerns where the impact is theoretical.\n\n"
        + _BASE_OUTPUT_CONTRACT
    ),
)

STYLE = Specialist(
    name="style",
    focus="readability, naming, comments, dead code, project conventions, type hints, error messages",
    system_prompt=(
        "You are the STYLE specialist on a code review squad.\n"
        "Your job is to find changes that hurt readability or violate the "
        "project's apparent conventions: confusing names, missing or misleading "
        "comments, dead code, inconsistent error messages, missing type hints "
        "where the surrounding code uses them, and obvious refactors that would "
        "make the diff easier to maintain.\n"
        "Skip purely subjective taste. Skip nits already covered by autoformat.\n\n"
        + _BASE_OUTPUT_CONTRACT
    ),
)

TEST_COVERAGE = Specialist(
    name="test_coverage",
    focus="missing tests, regression risk, brittle assertions, mocked-too-much, untested error paths",
    system_prompt=(
        "You are the TEST COVERAGE specialist on a code review squad.\n"
        "Your job is to flag where the diff introduces logic that is not "
        "exercised by tests, has weak or brittle assertions, mocks too much "
        "(hiding integration risk), or misses error-path coverage. If a test "
        "file is part of the diff, judge whether the new tests actually cover "
        "the new behavior in the same diff.\n"
        "Do not demand tests for trivial getters or pure docs. Be specific "
        "about which behavior or branch is uncovered.\n\n"
        + _BASE_OUTPUT_CONTRACT
    ),
)


SQUAD: list[Specialist] = [SECURITY, PERFORMANCE, STYLE, TEST_COVERAGE]
SQUAD_NAMES: tuple[str, ...] = tuple(s.name for s in SQUAD)


def get_specialist(name: str) -> Specialist | None:
    name = name.lower()
    for s in SQUAD:
        if s.name == name:
            return s
    return None


def select_squad(names: list[str] | tuple[str, ...] | None) -> list[Specialist]:
    """Resolve a list of specialist names into a Specialist subset.

    Empty / None → full squad. Unknown names raise ValueError so the CLI / API
    can surface a clean 4xx instead of silently dropping requested coverage.
    Order follows the canonical SQUAD order so reports stay deterministic.
    """
    if not names:
        return list(SQUAD)
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    if not wanted:
        return list(SQUAD)
    unknown = sorted(wanted - set(SQUAD_NAMES))
    if unknown:
        raise ValueError(
            f"unknown specialist(s): {', '.join(unknown)}. "
            f"valid: {', '.join(SQUAD_NAMES)}"
        )
    return [s for s in SQUAD if s.name in wanted]


def render_user_prompt(diff_bundle: str, repo_meta: dict[str, Any] | None = None) -> str:
    """Build the user prompt sent to every specialist alongside their system prompt."""
    meta = repo_meta or {}
    head = []
    if meta.get("repo"):
        head.append(f"Repository: {meta['repo']}")
    if meta.get("base") and meta.get("head"):
        head.append(f"Comparing: {meta['base']}..{meta['head']}")
    if meta.get("title"):
        head.append(f"PR title: {meta['title']}")
    if meta.get("description"):
        desc = meta["description"].strip()
        if len(desc) > 600:
            desc = desc[:600] + " ..."
        head.append(f"PR description:\n{desc}")
    head_block = "\n".join(head)
    return (
        f"{head_block}\n\n"
        f"Diff bundle (lines beginning with '+' are added, '-' are removed; "
        f"the integers are NEW-file line numbers for added lines and OLD-file "
        f"line numbers for removed lines):\n\n"
        f"{diff_bundle}\n\n"
        f"Return JSON only."
    )
