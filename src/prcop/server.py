"""HTTP server — exposes PR Cop as a small REST API plus a demo HTML page.

Endpoints:
    GET  /                       service metadata + biographer info
    GET  /health                  liveness
    GET  /provider                active LLM provider snapshot
    POST /review/diff             review a unified diff text body
    POST /review/github           review a GitHub PR (owner/repo/pr_number)
    GET  /demo                    static HTML demo
"""
from __future__ import annotations
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from prcop import __version__
from prcop.llm import provider_info
from prcop.orchestrator import (
    render_markdown_report,
    review_diff,
)
from prcop.sources import diff_from_github_pr, post_github_pr_comment

app = FastAPI(
    title="PR Cop",
    description="Multi-agent code review squad: 4 specialists + consensus, biographied by Xiaomi MiMo v2.5 Pro.",
    version=__version__,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class DiffReviewRequest(BaseModel):
    diff: str = Field(..., description="Unified diff text (e.g. output of `git diff base..head`).")
    repo: str | None = None
    base: str | None = None
    head: str | None = None
    title: str | None = None
    description: str | None = None


class GithubReviewRequest(BaseModel):
    owner: str
    repo: str
    pr_number: int
    post_comment: bool = False


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "PR Cop",
        "version": __version__,
        "description": "Multi-agent code review squad biographied by Xiaomi MiMo v2.5 Pro.",
        "biographer": {
            "default_provider": "mimo",
            "default_model": "mimo-v2.5-pro",
            "active_provider": os.environ.get("PRCOP_LLM_PROVIDER", "mimo"),
            "active_model": os.environ.get("PRCOP_LLM_MODEL") or "mimo-v2.5-pro",
        },
        "squad": ["security", "performance", "style", "test_coverage"],
        "endpoints": [
            "/health",
            "/provider",
            "/review/diff",
            "/review/github",
            "/demo",
        ],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


@app.get("/provider")
async def provider() -> dict[str, Any]:
    return provider_info()


_DEMO_PATH = Path(__file__).resolve().parents[2] / "demo" / "index.html"


@app.get("/demo")
async def demo() -> FileResponse:
    if not _DEMO_PATH.exists():
        raise HTTPException(status_code=404, detail="demo not bundled")
    return FileResponse(_DEMO_PATH, media_type="text/html")


@app.post("/review/diff")
async def review_diff_endpoint(req: DiffReviewRequest) -> JSONResponse:
    if not req.diff.strip():
        raise HTTPException(status_code=400, detail="empty diff")
    repo_meta = {
        "repo": req.repo,
        "base": req.base,
        "head": req.head,
        "title": req.title,
        "description": req.description,
    }
    repo_meta = {k: v for k, v in repo_meta.items() if v}
    result = await review_diff(diff_text=req.diff, repo_meta=repo_meta)
    return JSONResponse(content=result.to_dict())


@app.post("/review/github")
async def review_github_endpoint(req: GithubReviewRequest) -> JSONResponse:
    try:
        src = await diff_from_github_pr(req.owner, req.repo, req.pr_number)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub fetch failed: {e}") from e
    result = await review_diff(diff_text=src.diff_text, repo_meta=src.repo_meta)
    payload = result.to_dict()
    if req.post_comment:
        body = render_markdown_report(result)
        try:
            comment = await post_github_pr_comment(
                req.owner, req.repo, req.pr_number, body
            )
            payload["github_comment_url"] = comment.get("html_url")
        except Exception as e:
            payload["github_comment_error"] = str(e)
    return JSONResponse(content=payload)


def serve() -> None:
    """Console-script entry: ``prcop serve``."""
    import uvicorn
    host = os.environ.get("PRCOP_HOST", "0.0.0.0")
    port = int(os.environ.get("PRCOP_PORT", "8080"))
    uvicorn.run("prcop.server:app", host=host, port=port, reload=False)
