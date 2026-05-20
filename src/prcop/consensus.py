"""Consensus pass.

After all specialists run in parallel, we have N findings of varying quality.
The consensus pass:

1. Deduplicates near-identical findings (same file + nearby line + same category).
2. Boosts severity when multiple specialists agree on the same finding.
3. Asks the biographer model (MiMo v2.5 Pro by default) to write a single
   review verdict that pulls the top issues into a one-paragraph human
   summary suitable for a GitHub PR comment.

Step 3 is what makes PR Cop feel like a single reviewer rather than four
shouting agents.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from prcop.llm import call_chat
from prcop.specialists import SEVERITY_RANK, Finding


def _key(finding: Finding) -> tuple[str, str, int]:
    """Bucket a finding by file, category, and line group (within 3 lines)."""
    line = finding.line if finding.line is not None else -1
    return (finding.file, finding.category.lower(), line // 3)


def deduplicate(findings: list[Finding]) -> list[Finding]:
    """Merge near-duplicate findings.  When two specialists flag the same
    file+line+category, we keep the longest rationale, union the specialists,
    and bump the severity by one rung if both agreed it was a real issue.
    """
    buckets: dict[tuple[str, str, int], Finding] = {}
    for f in findings:
        k = _key(f)
        existing = buckets.get(k)
        if existing is None:
            buckets[k] = Finding(**asdict(f))
            continue
        # Merge: keep stronger severity, accumulate specialists in the name field,
        # keep the longer rationale, and bump severity when distinct specialists agreed.
        merged_specialists = sorted(set(
            existing.specialist.split("+") + [f.specialist]
        ))
        existing.specialist = "+".join(merged_specialists)
        if SEVERITY_RANK[f.severity] < SEVERITY_RANK[existing.severity]:
            existing.severity = f.severity
        if len(f.rationale) > len(existing.rationale):
            existing.rationale = f.rationale
        if f.suggestion and (not existing.suggestion or len(f.suggestion) > len(existing.suggestion)):
            existing.suggestion = f.suggestion
        # Multi-specialist agreement bumps severity one rung (capped at critical).
        if len(merged_specialists) >= 2:
            cur_rank = SEVERITY_RANK[existing.severity]
            if cur_rank > 0:
                existing.severity = list(SEVERITY_RANK)[cur_rank - 1]
    return list(buckets.values())


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (SEVERITY_RANK[f.severity], f.file, f.line if f.line is not None else 0),
    )


_VERDICT_SYSTEM = """You are PR Cop, the consensus voice of a 4-agent code
review squad (security, performance, style, test_coverage). Read the merged
findings and write a single concise review comment for the PR author.

Rules:
- Lead with a one-line verdict: "Approve", "Approve with nits", "Request changes", or "Block".
- Then 2-4 short paragraphs (no bullet lists). Cite at most the 5 most important findings by file:line.
- Tone: direct, factual, slightly observational. No hype, no padding, no apology.
- Reference real numbers from the inputs (file count, finding count, severities).
- Do not invent issues. If the only findings are info/low, the verdict is Approve or Approve with nits.
- Output plain text. No markdown headers. No JSON.
""".strip()


def _verdict_user_prompt(
    findings: list[Finding],
    file_count: int,
    repo_meta: dict[str, Any] | None,
) -> str:
    meta = repo_meta or {}
    sev_counts = {s: 0 for s in SEVERITY_RANK}
    for f in findings:
        sev_counts[f.severity] += 1

    summary_lines = [
        f"Files reviewed: {file_count}",
        f"Findings: total={len(findings)} | "
        + ", ".join(f"{k}={v}" for k, v in sev_counts.items() if v),
    ]
    if meta.get("title"):
        summary_lines.append(f"PR title: {meta['title']}")

    head = "\n".join(summary_lines)

    detail = json.dumps(
        [
            {
                "specialist": f.specialist,
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "rationale": f.rationale[:300],
            }
            for f in findings[:25]
        ],
        ensure_ascii=False, indent=1,
    )
    return f"{head}\n\nMerged findings (top 25):\n{detail}\n\nWrite the verdict comment now."


async def write_verdict(
    findings: list[Finding],
    *,
    file_count: int,
    repo_meta: dict[str, Any] | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> str:
    """Ask the LLM to produce a single human-readable PR review comment."""
    if not findings:
        return (
            "Approve. No issues flagged across security, performance, style, "
            "or test coverage. The diff looks clean and self-contained."
        )
    user = _verdict_user_prompt(findings, file_count, repo_meta)
    text = await call_chat(
        system=_VERDICT_SYSTEM,
        user=user,
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        json_mode=False,
        temperature=0.3,
        max_tokens=600,
    )
    return text.strip()
