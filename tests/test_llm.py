"""Tests for the LLM module: JSON cleanup + repair + provider_info snapshot."""
from __future__ import annotations

import json

import pytest

from prcop.llm import (
    ProviderError,
    _repair_json,
    _strip_to_json,
    call_json,
    provider_info,
)

# -----------------------------------------------------------------------------
# _strip_to_json
# -----------------------------------------------------------------------------


def test_strip_removes_json_fence():
    raw = '```json\n{"findings": []}\n```'
    assert _strip_to_json(raw) == '{"findings": []}'


def test_strip_removes_plain_fence():
    raw = '```\n{"a": 1}\n```'
    assert _strip_to_json(raw) == '{"a": 1}'


def test_strip_drops_leading_commentary():
    raw = "Sure! Here is the JSON:\n{\"findings\": [1, 2]}\nthanks"
    out = _strip_to_json(raw)
    assert out.startswith("{")
    assert out.endswith("]}")


def test_strip_handles_array_payload():
    raw = "Output:\n[1, 2, 3]\nbye"
    assert _strip_to_json(raw) == "[1, 2, 3]"


def test_strip_empty_string_passthrough():
    assert _strip_to_json("") == ""


def test_strip_no_json_returns_input():
    assert _strip_to_json("just words, no braces") == "just words, no braces"


# -----------------------------------------------------------------------------
# _repair_json — trailing commas
# -----------------------------------------------------------------------------


def test_repair_strips_trailing_comma_object():
    assert _repair_json('{"a": 1,}') == '{"a": 1}'


def test_repair_strips_trailing_comma_array():
    assert _repair_json("[1, 2, 3,]") == "[1, 2, 3]"


def test_repair_strips_nested_trailing_commas():
    raw = '{"findings": [{"a": 1,}, {"b": 2,},]}'
    assert _repair_json(raw) == '{"findings": [{"a": 1}, {"b": 2}]}'


def test_repair_leaves_valid_json_alone():
    raw = '{"a": 1, "b": 2}'
    assert _repair_json(raw) == raw


# -----------------------------------------------------------------------------
# call_json — full pipeline through both repair passes
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_json_handles_fenced_response(monkeypatch):
    async def fake_chat(**_):
        return '```json\n{"findings": [{"file": "a", "title": "t"}]}\n```'

    monkeypatch.setattr("prcop.llm.call_chat", fake_chat)
    out = await call_json(system="s", user="u")
    assert out == {"findings": [{"file": "a", "title": "t"}]}


@pytest.mark.asyncio
async def test_call_json_handles_trailing_comma_after_strip(monkeypatch):
    async def fake_chat(**_):
        # both: leading commentary AND trailing comma → needs strip + repair
        return 'Sure thing:\n{"findings": [{"file": "x",},]}'

    monkeypatch.setattr("prcop.llm.call_chat", fake_chat)
    out = await call_json(system="s", user="u")
    assert out == {"findings": [{"file": "x"}]}


@pytest.mark.asyncio
async def test_call_json_raises_on_unrecoverable_garbage(monkeypatch):
    async def fake_chat(**_):
        return "this is just prose with no json at all"

    monkeypatch.setattr("prcop.llm.call_chat", fake_chat)
    with pytest.raises(ProviderError):
        await call_json(system="s", user="u")


@pytest.mark.asyncio
async def test_call_json_passes_through_clean_payload(monkeypatch):
    payload = {"findings": [{"file": "a", "line": 12, "severity": "low", "title": "t"}]}

    async def fake_chat(**_):
        return json.dumps(payload)

    monkeypatch.setattr("prcop.llm.call_chat", fake_chat)
    out = await call_json(system="s", user="u")
    assert out == payload


# -----------------------------------------------------------------------------
# provider_info — env snapshot
# -----------------------------------------------------------------------------


def test_provider_info_defaults_to_mimo_mock_without_key(monkeypatch):
    for k in ("PRCOP_LLM_PROVIDER", "PRCOP_LLM_API_KEY", "PRCOP_LLM_MODEL", "PRCOP_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    info = provider_info()
    assert info["configured_provider"] == "mimo"
    assert info["effective_provider"] == "mock"
    assert info["api_key_present"] is False
    assert info["default_model"] == "mimo-v2.5-pro"


def test_provider_info_effective_matches_when_key_present(monkeypatch):
    monkeypatch.setenv("PRCOP_LLM_PROVIDER", "mimo")
    monkeypatch.setenv("PRCOP_LLM_API_KEY", "k")
    info = provider_info()
    assert info["effective_provider"] == "mimo"
    assert info["api_key_present"] is True


def test_provider_info_gateway_does_not_require_key(monkeypatch):
    monkeypatch.setenv("PRCOP_LLM_PROVIDER", "gateway")
    monkeypatch.delenv("PRCOP_LLM_API_KEY", raising=False)
    info = provider_info()
    assert info["effective_provider"] == "gateway"
