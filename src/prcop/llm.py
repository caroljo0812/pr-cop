"""LLM provider layer.

Default provider: **Xiaomi MiMo v2.5 Pro** (mimo-v2.5-pro). MiMo is reasoning
strong and code aware which is exactly what specialist code review agents need:
read a structured diff bundle, return a strict JSON list of issues with file
paths, line numbers, severity, and a short explanation.

Supports multiple providers, configured via env:
  PRCOP_LLM_PROVIDER = mimo | openai | together | gateway | mock
  PRCOP_LLM_API_KEY  = <provider key>
  PRCOP_LLM_MODEL    = <model name, default mimo-v2.5-pro for mimo>
  PRCOP_LLM_BASE_URL = <override base url, optional>

Each call asks the provider for a JSON response. We don't trust the model to
emit valid JSON every time, so the layer attempts repair before failing.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx


class ProviderError(RuntimeError):
    """Raised when an LLM provider fails to return usable content."""


# Single shared client — reuses connections across all specialist calls. Lazily
# constructed so import-time doesn't open sockets, and replaceable from tests.
_CLIENT: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _CLIENT


async def aclose_client() -> None:
    """Close the shared client. Call from app lifespan / test teardown."""
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None


def _strip_to_json(text: str) -> str:
    """Best-effort cleanup so we can ``json.loads`` model output.

    Models love to wrap JSON in ```json fences and add commentary.  We strip
    fences, find the first { or [ and the matching last } or ], and trim.
    """
    if not text:
        return text
    # Remove triple-backtick fences entirely (with or without language tag).
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    # Find the first JSON-y opener.
    m = re.search(r"[\[{]", text)
    if not m:
        return text
    start = m.start()
    # Find the matching last closer.
    last = max(text.rfind("]"), text.rfind("}"))
    if last <= start:
        return text[start:]
    return text[start:last + 1]


def _repair_json(text: str) -> str:
    """Apply additional repairs: strip trailing commas before } or ]."""
    # Common LLM mistake: {"a": 1,} or [1, 2,]
    return re.sub(r",(\s*[}\]])", r"\1", text)


async def call_chat(
    *,
    system: str,
    user: str,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    json_mode: bool = True,
    timeout: float = 90.0,
) -> str:
    """Call an OpenAI-compatible chat endpoint and return the assistant text.

    Defaults route to MiMo v2.5 Pro. Returns the raw assistant text — callers
    parse JSON themselves so they can apply schema-specific recovery.
    """
    provider = (provider or os.environ.get("PRCOP_LLM_PROVIDER") or "mimo").lower()
    api_key = api_key or os.environ.get("PRCOP_LLM_API_KEY") or ""
    model = model or os.environ.get("PRCOP_LLM_MODEL") or ""
    base_url = base_url or os.environ.get("PRCOP_LLM_BASE_URL") or ""
    max_tokens = max_tokens or int(os.environ.get("PRCOP_MAX_TOKENS", "1200"))

    if provider == "mock":
        # Deterministic offline output for tests / demos.
        if not json_mode:
            return (
                "Approve with nits. Mock reviewer engaged — wire a real provider "
                "(PRCOP_LLM_PROVIDER=mimo + PRCOP_LLM_API_KEY) to get an "
                "actual verdict."
            )
        return json.dumps({
            "findings": [
                {
                    "file": "<mock>",
                    "line": 1,
                    "severity": "info",
                    "category": "mock",
                    "title": "Mock provider produced this output",
                    "rationale": "PRCOP_LLM_PROVIDER=mock — wire a real provider for real reviews.",
                }
            ]
        })

    if provider != "gateway" and not api_key:
        # Soft fallback: keep the pipeline runnable offline by returning the
        # mock payload rather than raising. CLI surfaces this clearly.
        return await call_chat(
            system=system, user=user, provider="mock",
            max_tokens=max_tokens, temperature=temperature, json_mode=json_mode,
        )

    if provider == "gateway":
        if not base_url:
            raise ProviderError("PRCOP_LLM_BASE_URL is required for the gateway provider")
        url = base_url.rstrip("/") + "/v1/chat/completions"
        model = model or "mimo-v2.5-pro"
        # gateway may use upstream auth — only set Authorization if we have a key.
    elif provider == "mimo":
        url = (base_url or "https://api.xiaomimimo.com").rstrip("/") + "/v1/chat/completions"
        model = model or "mimo-v2.5-pro"
    elif provider == "openai":
        url = (base_url or "https://api.openai.com").rstrip("/") + "/v1/chat/completions"
        model = model or "gpt-4o-mini"
    elif provider == "together":
        url = "https://api.together.xyz/v1/chat/completions"
        model = model or "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    else:
        raise ProviderError(f"Unknown LLM provider: {provider}")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    client = _client()
    last_status: int | None = None
    last_body: str = ""
    # Retry transient failures: 429 (rate limit), 502/503/504 (upstream).
    for attempt in range(3):
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=timeout)
        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            if attempt == 2:
                raise ProviderError(f"{provider} unreachable: {e}") from e
            await asyncio.sleep(0.5 * (2 ** attempt))
            continue
        last_status = r.status_code
        last_body = r.text
        if r.status_code == 200:
            data = r.json()
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as e:
                raise ProviderError(f"{provider}: malformed response shape") from e
        # Some MiMo / gateway combos reject `response_format`; retry without it.
        if json_mode and r.status_code in (400, 422):
            payload.pop("response_format", None)
            json_mode = False
            continue
        if r.status_code in (429, 502, 503, 504) and attempt < 2:
            # Honor Retry-After when present, else exponential backoff.
            ra = r.headers.get("Retry-After")
            try:
                delay = float(ra) if ra else 0.5 * (2 ** attempt)
            except ValueError:
                delay = 0.5 * (2 ** attempt)
            await asyncio.sleep(min(delay, 5.0))
            continue
        # Non-retryable error.
        break
    raise ProviderError(f"{provider} returned {last_status}: {last_body[:300]}")


async def call_json(
    *,
    system: str,
    user: str,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> Any:
    """Call the chat endpoint and parse the response as JSON.

    Applies a few light recovery passes so we don't fail on minor model quirks
    (markdown fences, leading commentary, trailing text after JSON).
    """
    raw = await call_chat(
        system=system, user=user, provider=provider, api_key=api_key,
        model=model, base_url=base_url, max_tokens=max_tokens,
        temperature=temperature, json_mode=True, timeout=timeout,
    )
    cleaned = _strip_to_json(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try repair (trailing commas) before giving up.
        try:
            return json.loads(_repair_json(cleaned))
        except json.JSONDecodeError as e:
            raise ProviderError(f"non-JSON response: {cleaned[:200]}") from e


def provider_info() -> dict[str, Any]:
    """Snapshot the active provider config for /provider, --status, etc."""
    provider = os.environ.get("PRCOP_LLM_PROVIDER", "mimo").lower()
    api_key_set = bool(os.environ.get("PRCOP_LLM_API_KEY"))
    base_url = os.environ.get("PRCOP_LLM_BASE_URL") or None
    model = os.environ.get("PRCOP_LLM_MODEL") or (
        "mimo-v2.5-pro" if provider in ("mimo", "gateway") else "gpt-4o-mini"
    )
    effective = provider
    if provider not in ("gateway", "mock") and not api_key_set:
        effective = "mock"
    return {
        "configured_provider": provider,
        "configured_model": model,
        "configured_base_url": base_url,
        "api_key_present": api_key_set,
        "effective_provider": effective,
        "default_provider": "mimo",
        "default_model": "mimo-v2.5-pro",
        "supported_providers": ["mimo", "openai", "together", "gateway", "mock"],
    }
