"""Orchestrator — fan out the diff to all specialists in parallel, then run consensus.

Public entry point: ``review_diff()`` returns a ``ReviewResult`` containing the
deduplicated findings plus the consensus verdict text.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from prcop.consensus import deduplicate, sort_findings, write_verdict
from prcop.diff import FileDiff, parse_diff, render_diff_bundle
from prcop.llm import ProviderError, call_json, provider_info
from prcop.specialists import (
    SQUAD,
    Finding,
    Specialist,
    render_user_prompt,
)


@dataclass
class SpecialistRun:
    specialist: str
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0


@dataclass
class ReviewResult:
    findings: list[Finding]
    verdict: str
    runs: list[SpecialistRun]
    file_count: int
    duration_ms: int
    provider: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "file_count": self.file_count,
            "finding_count": len(self.findings),
            "duration_ms": self.duration_ms,
            "provider": self.provider,
            "specialists": [
                {
                    "name": r.specialist,
                    "findings": len(r.findings),
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                }
                for r in self.runs
            ],
            "findings": [f.to_dict() for f in self.findings],
        }


async def _run_specialist(
    spec: Specialist,
    diff_bundle: str,
    repo_meta: dict[str, Any] | None,
    provider: str | None,
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    max_tokens: int | None,
) -> SpecialistRun:
    started = time.monotonic()
    user = render_user_prompt(diff_bundle, repo_meta)
    user += (
        f"\n\nReturn at most {spec.max_findings} findings. Focus area: {spec.focus}."
    )
    try:
        data = await call_json(
            system=spec.system_prompt,
            user=user,
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            max_tokens=max_tokens,
        )
    except ProviderError as e:
        return SpecialistRun(
            specialist=spec.name,
            error=str(e),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    raw_findings: list[dict[str, Any]] = []
    if isinstance(data, dict):
        raw_findings = data.get("findings") or []
    elif isinstance(data, list):
        raw_findings = data
    findings: list[Finding] = []
    for item in raw_findings[: spec.max_findings]:
        if not isinstance(item, dict):
            continue
        # Force the specialist tag so consensus can attribute correctly even if
        # the LLM mislabeled it.
        item.setdefault("specialist", spec.name)
        item["specialist"] = spec.name
        findings.append(Finding.from_dict(item, default_specialist=spec.name))
    return SpecialistRun(
        specialist=spec.name,
        findings=findings,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def review_diff(
    *,
    diff_text: str,
    repo_meta: dict[str, Any] | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_tokens: int | None = None,
    concurrency: int | None = None,
) -> ReviewResult:
    """Run the full review pipeline against a unified diff string.

    Steps: parse → render → fan out to specialists in parallel → dedupe →
    write consensus verdict via the biographer model.
    """
    started = time.monotonic()
    files = parse_diff(diff_text)
    bundle = render_diff_bundle(files)

    # Run specialists in parallel. Concurrency caps total in-flight HTTP calls
    # to the LLM provider so we don't get rate-limited on cheap free tiers.
    concurrency = concurrency or int(os.environ.get("PRCOP_CONCURRENCY", str(len(SQUAD))))
    sem = asyncio.Semaphore(concurrency)

    async def guarded(spec: Specialist) -> SpecialistRun:
        async with sem:
            return await _run_specialist(
                spec, bundle, repo_meta, provider, api_key, model, base_url, max_tokens
            )

    runs = await asyncio.gather(*[guarded(s) for s in SQUAD])

    all_findings: list[Finding] = []
    for r in runs:
        all_findings.extend(r.findings)
    merged = sort_findings(deduplicate(all_findings))

    verdict = await write_verdict(
        merged,
        file_count=len(files),
        repo_meta=repo_meta,
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )

    return ReviewResult(
        findings=merged,
        verdict=verdict,
        runs=runs,
        file_count=len(files),
        duration_ms=int((time.monotonic() - started) * 1000),
        provider=provider_info(),
    )


async def review_diff_files(
    files: list[FileDiff],
    *,
    repo_meta: dict[str, Any] | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_tokens: int | None = None,
    concurrency: int | None = None,
) -> ReviewResult:
    """Variant that takes pre-parsed FileDiff objects (used by /review/files HTTP endpoint)."""
    diff_text = "\n".join(f.render_for_review() for f in files)
    # We reuse review_diff so the renderer-aware semantics stay identical.
    return await review_diff(
        diff_text=diff_text,
        repo_meta=repo_meta,
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        max_tokens=max_tokens,
        concurrency=concurrency,
    )


def render_text_report(result: ReviewResult) -> str:
    """Pretty plain-text rendering for CLI / GitHub PR comment fallback."""
    lines: list[str] = []
    p = result.provider
    lines.append("PR COP REVIEW")
    lines.append("=" * 60)
    lines.append(f"Files: {result.file_count}  |  Findings: {len(result.findings)}  |  "
                 f"Duration: {result.duration_ms} ms")
    lines.append(
        f"Reviewer: {p['effective_provider']} / {p['configured_model']}  "
        f"(default: {p['default_provider']} / {p['default_model']})"
    )
    lines.append("")
    lines.append(result.verdict)
    if not result.findings:
        return "\n".join(lines)
    lines.append("")
    lines.append("FINDINGS")
    lines.append("-" * 60)
    for f in result.findings:
        loc = f.file
        if f.line:
            loc += f":{f.line}"
        lines.append(f"[{f.severity.upper()}] [{f.specialist}] {f.title}")
        lines.append(f"  at {loc}  ({f.category})")
        lines.append(f"  {f.rationale}")
        if f.suggestion:
            lines.append(f"  suggest: {f.suggestion}")
        lines.append("")
    return "\n".join(lines)


def render_markdown_report(result: ReviewResult, *, include_json: bool = False) -> str:
    """Markdown rendering for posting as a GitHub PR comment."""
    p = result.provider
    out: list[str] = []
    out.append("## PR Cop review")
    out.append("")
    out.append(result.verdict)
    out.append("")
    out.append(
        f"<sub>Reviewed by {p['effective_provider']} / `{p['configured_model']}`. "
        f"{result.file_count} file(s), {len(result.findings)} finding(s), "
        f"{result.duration_ms} ms.</sub>"
    )
    if result.findings:
        out.append("")
        out.append("### Findings")
        out.append("")
        out.append("| sev | specialist | location | category | issue |")
        out.append("|---|---|---|---|---|")
        for f in result.findings:
            loc = f.file + (f":{f.line}" if f.line else "")
            title = f.title.replace("|", "\\|")
            out.append(
                f"| `{f.severity}` | {f.specialist} | `{loc}` | {f.category} | {title} |"
            )
    if include_json:
        out.append("")
        out.append("<details><summary>raw JSON</summary>\n")
        out.append("```json")
        out.append(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        out.append("```")
        out.append("</details>")
    return "\n".join(out)
