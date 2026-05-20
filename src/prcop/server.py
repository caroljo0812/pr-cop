"""HTTP server — exposes PR Cop as a small REST API plus a demo HTML page.

Endpoints:
    GET  /                       service metadata + biographer info
    GET  /health                  liveness
    GET  /provider                active LLM provider snapshot
    POST /review/diff             review a unified diff text body
    POST /review/github           review a GitHub PR (owner/repo/pr_number)
    GET  /demo                    static HTML demo

Safety env vars:
    PRCOP_API_KEY            if set, every /review/* call must send
                              ``X-PRCOP-API-Key: <value>`` (bearer also accepted).
    PRCOP_MAX_DIFF_BYTES     reject diff bodies larger than N bytes (default 1 MiB).
    PRCOP_CORS_ORIGINS       comma-separated allowlist (default ``*`` for the
                              demo; tighten to your front-end origin in prod).
    PRCOP_HOST               default ``127.0.0.1``. Set ``0.0.0.0`` only when
                              you've put auth in front (PRCOP_API_KEY or a
                              reverse proxy).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
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

# --- safety knobs --------------------------------------------------------

DEFAULT_MAX_DIFF_BYTES = 1024 * 1024  # 1 MiB


def _max_diff_bytes() -> int:
    raw = os.environ.get("PRCOP_MAX_DIFF_BYTES")
    if not raw:
        return DEFAULT_MAX_DIFF_BYTES
    try:
        n = int(raw)
        return n if n > 0 else DEFAULT_MAX_DIFF_BYTES
    except ValueError:
        return DEFAULT_MAX_DIFF_BYTES


def _cors_origins() -> list[str]:
    raw = os.environ.get("PRCOP_CORS_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _expected_api_key() -> str | None:
    """Return the configured API key, or None if auth is disabled."""
    key = os.environ.get("PRCOP_API_KEY", "").strip()
    return key or None


async def require_api_key(
    request: Request,
    x_prcop_api_key: str | None = Header(default=None, alias="X-PRCOP-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """Gate /review/* when PRCOP_API_KEY is set. No-op when unset."""
    expected = _expected_api_key()
    if not expected:
        return
    candidate: str | None = None
    if x_prcop_api_key:
        candidate = x_prcop_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    if not candidate or candidate != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    # mark for visibility in logs without echoing the value
    request.state.authed = True


# --- app ----------------------------------------------------------------

app = FastAPI(
    title="PR Cop",
    description="Multi-agent code review squad: 4 specialists + consensus, biographied by Xiaomi MiMo v2.5 Pro.",
    version=__version__,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
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
    specialists: list[str] | None = Field(
        default=None,
        description="Optional subset of specialists to run. Choices: security, performance, style, test_coverage.",
    )


class GithubReviewRequest(BaseModel):
    owner: str
    repo: str
    pr_number: int
    post_comment: bool = False
    specialists: list[str] | None = Field(
        default=None,
        description="Optional subset of specialists to run.",
    )


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
        "limits": {
            "max_diff_bytes": _max_diff_bytes(),
            "auth_required": _expected_api_key() is not None,
        },
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


@app.get("/provider")
async def provider() -> dict[str, Any]:
    return provider_info()


_DEMO_PATH = Path(__file__).resolve().parent / "demo.html"


@app.get("/demo")
async def demo() -> FileResponse:
    if not _DEMO_PATH.exists():
        raise HTTPException(status_code=404, detail="demo not bundled")
    return FileResponse(_DEMO_PATH, media_type="text/html")


@app.post("/review/diff", dependencies=[Depends(require_api_key)])
async def review_diff_endpoint(req: DiffReviewRequest) -> JSONResponse:
    if not req.diff.strip():
        raise HTTPException(status_code=400, detail="empty diff")
    cap = _max_diff_bytes()
    diff_size = len(req.diff.encode("utf-8"))
    if diff_size > cap:
        raise HTTPException(
            status_code=413,
            detail=f"diff too large: {diff_size} bytes > limit {cap} bytes "
                   f"(set PRCOP_MAX_DIFF_BYTES to override)",
        )
    repo_meta = {
        "repo": req.repo,
        "base": req.base,
        "head": req.head,
        "title": req.title,
        "description": req.description,
    }
    repo_meta = {k: v for k, v in repo_meta.items() if v}
    try:
        result = await review_diff(
            diff_text=req.diff,
            repo_meta=repo_meta,
            specialists=req.specialists,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse(content=result.to_dict())


@app.post("/review/github", dependencies=[Depends(require_api_key)])
async def review_github_endpoint(req: GithubReviewRequest) -> JSONResponse:
    try:
        src = await diff_from_github_pr(req.owner, req.repo, req.pr_number)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub fetch failed: {e}") from e
    cap = _max_diff_bytes()
    diff_size = len(src.diff_text.encode("utf-8"))
    if diff_size > cap:
        raise HTTPException(
            status_code=413,
            detail=f"PR diff too large: {diff_size} bytes > limit {cap} bytes",
        )
    try:
        result = await review_diff(
            diff_text=src.diff_text,
            repo_meta=src.repo_meta,
            specialists=req.specialists,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
    host = os.environ.get("PRCOP_HOST", "127.0.0.1")
    port = int(os.environ.get("PRCOP_PORT", "8080"))
    uvicorn.run("prcop.server:app", host=host, port=port, reload=False)
