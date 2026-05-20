"""Tests for server safeguards: API key gate + max diff size + bind defaults."""
from __future__ import annotations

from importlib import reload

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_app(monkeypatch):
    """Re-import prcop.server with a clean env so module-level config rebinds."""
    def _build(env: dict[str, str] | None = None):
        for k in ("PRCOP_API_KEY", "PRCOP_MAX_DIFF_BYTES", "PRCOP_CORS_ORIGINS", "PRCOP_HOST"):
            monkeypatch.delenv(k, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        # reload so CORS origins (read at import time) and helpers re-read env
        import prcop.server as srv
        reload(srv)
        return srv
    return _build


def _stub_review_diff(monkeypatch, server_module):
    """Replace review_diff with a fast stub so tests don't hit any LLM."""
    class _Result:
        def to_dict(self):
            return {"verdict": "ok", "findings": []}

    async def _fake(diff_text, repo_meta=None, specialists=None, **_):
        return _Result()

    monkeypatch.setattr(server_module, "review_diff", _fake)


def test_review_diff_open_when_no_api_key(fresh_app, monkeypatch):
    srv = fresh_app()
    _stub_review_diff(monkeypatch, srv)
    client = TestClient(srv.app)
    r = client.post("/review/diff", json={"diff": "--- a\n+++ b\n@@\n+x\n"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "ok"


def test_review_diff_requires_api_key_when_set(fresh_app, monkeypatch):
    srv = fresh_app({"PRCOP_API_KEY": "s3cret"})
    _stub_review_diff(monkeypatch, srv)
    client = TestClient(srv.app)
    r = client.post("/review/diff", json={"diff": "--- a\n+++ b\n@@\n+x\n"})
    assert r.status_code == 401


def test_review_diff_accepts_x_prcop_header(fresh_app, monkeypatch):
    srv = fresh_app({"PRCOP_API_KEY": "s3cret"})
    _stub_review_diff(monkeypatch, srv)
    client = TestClient(srv.app)
    r = client.post(
        "/review/diff",
        json={"diff": "--- a\n+++ b\n@@\n+x\n"},
        headers={"X-PRCOP-API-Key": "s3cret"},
    )
    assert r.status_code == 200


def test_review_diff_accepts_bearer_authorization(fresh_app, monkeypatch):
    srv = fresh_app({"PRCOP_API_KEY": "s3cret"})
    _stub_review_diff(monkeypatch, srv)
    client = TestClient(srv.app)
    r = client.post(
        "/review/diff",
        json={"diff": "--- a\n+++ b\n@@\n+x\n"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 200


def test_review_diff_rejects_wrong_api_key(fresh_app, monkeypatch):
    srv = fresh_app({"PRCOP_API_KEY": "s3cret"})
    _stub_review_diff(monkeypatch, srv)
    client = TestClient(srv.app)
    r = client.post(
        "/review/diff",
        json={"diff": "--- a\n+++ b\n@@\n+x\n"},
        headers={"X-PRCOP-API-Key": "wrong"},
    )
    assert r.status_code == 401


def test_health_does_not_require_api_key(fresh_app, monkeypatch):
    srv = fresh_app({"PRCOP_API_KEY": "s3cret"})
    client = TestClient(srv.app)
    assert client.get("/health").status_code == 200
    assert client.get("/provider").status_code == 200
    assert client.get("/").status_code == 200


def test_review_diff_413_when_diff_exceeds_cap(fresh_app, monkeypatch):
    srv = fresh_app({"PRCOP_MAX_DIFF_BYTES": "256"})
    _stub_review_diff(monkeypatch, srv)
    client = TestClient(srv.app)
    big = "--- a\n+++ b\n@@\n" + ("+x" * 500) + "\n"
    r = client.post("/review/diff", json={"diff": big})
    assert r.status_code == 413
    assert "diff too large" in r.json()["detail"]


def test_root_advertises_limits_and_auth(fresh_app):
    srv = fresh_app({"PRCOP_API_KEY": "s3cret", "PRCOP_MAX_DIFF_BYTES": "4096"})
    client = TestClient(srv.app)
    body = client.get("/").json()
    assert body["limits"]["max_diff_bytes"] == 4096
    assert body["limits"]["auth_required"] is True


def test_empty_diff_still_400(fresh_app, monkeypatch):
    srv = fresh_app()
    _stub_review_diff(monkeypatch, srv)
    client = TestClient(srv.app)
    r = client.post("/review/diff", json={"diff": "   "})
    assert r.status_code == 400
