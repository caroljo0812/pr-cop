"""Helpers for sourcing diffs.

We support three sources:

1. **Local repository** — produce a diff between two refs via GitPython.
2. **GitHub PR** — fetch the unified diff from the GitHub REST API
   (``application/vnd.github.v3.diff``).
3. **stdin / file** — caller supplies the diff text directly.

These helpers are intentionally side-effect free apart from network/git reads,
so they're easy to swap for fixtures in tests.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class DiffSource:
    diff_text: str
    repo_meta: dict[str, Any]


def diff_from_text(text: str, *, repo_meta: dict[str, Any] | None = None) -> DiffSource:
    return DiffSource(diff_text=text, repo_meta=repo_meta or {})


def diff_from_file(path: str | Path, *, repo_meta: dict[str, Any] | None = None) -> DiffSource:
    p = Path(path)
    return DiffSource(diff_text=p.read_text(encoding="utf-8"), repo_meta=repo_meta or {"source": str(p)})


def diff_from_local_repo(repo_path: str | Path, base: str, head: str = "HEAD") -> DiffSource:
    """Use GitPython to compute a unified diff between ``base`` and ``head``."""
    from git import Repo  # local import keeps top-level optional
    repo = Repo(str(repo_path))
    diff_text = repo.git.diff(f"{base}..{head}", "--unified=3")
    meta = {
        "repo": Path(repo_path).resolve().name,
        "base": base,
        "head": head,
        "source": "local",
    }
    return DiffSource(diff_text=diff_text, repo_meta=meta)


async def diff_from_github_pr(
    owner: str, repo: str, pr_number: int, *, token: str | None = None,
) -> DiffSource:
    """Fetch the unified diff and PR metadata from GitHub.

    Auth is optional for public repos but recommended to avoid the 60 req/hour
    unauthenticated rate limit. Pass ``token`` directly or set
    ``PRCOP_GITHUB_TOKEN`` / ``GITHUB_TOKEN``.
    """
    token = token or os.environ.get("PRCOP_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers_diff = {
        "Accept": "application/vnd.github.v3.diff",
        "User-Agent": "pr-cop/0.1",
    }
    headers_json = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pr-cop/0.1",
    }
    if token:
        headers_diff["Authorization"] = f"token {token}"
        headers_json["Authorization"] = f"token {token}"

    pr_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        meta_r = await client.get(pr_url, headers=headers_json)
        meta_r.raise_for_status()
        meta = meta_r.json()
        diff_r = await client.get(pr_url, headers=headers_diff)
        diff_r.raise_for_status()
        diff_text = diff_r.text

    repo_meta = {
        "repo": f"{owner}/{repo}",
        "pr": pr_number,
        "title": meta.get("title"),
        "description": (meta.get("body") or "")[:2000],
        "base": meta.get("base", {}).get("ref"),
        "head": meta.get("head", {}).get("ref"),
        "html_url": meta.get("html_url"),
        "source": "github",
    }
    return DiffSource(diff_text=diff_text, repo_meta=repo_meta)


async def post_github_pr_comment(
    owner: str, repo: str, pr_number: int, body: str, *, token: str | None = None,
) -> dict[str, Any]:
    """Post an issue-style comment on a PR. Requires ``repo`` scope on the token."""
    token = token or os.environ.get("PRCOP_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("PRCOP_GITHUB_TOKEN required to post PR comments")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
        "User-Agent": "pr-cop/0.1",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json={"body": body}, headers=headers)
        r.raise_for_status()
        return r.json()
