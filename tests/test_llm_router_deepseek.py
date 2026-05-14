"""
test_llm_router_deepseek.py — DeepSeek tier wiring (no real network calls).

Verifies:
  - tier order respects ALLOW_DEEPSEEK / KOLAW_PREFER_DEEPSEEK env flags
  - _call_deepseek hits the right URL with bearer auth (mocked transport)
  - missing key raises before any network attempt
"""

import asyncio
import importlib

import httpx
import pytest


def _reload_router(env: dict[str, str], monkeypatch):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import services.llm.router as router_mod
    return importlib.reload(router_mod)


def test_default_tier_is_local_only(monkeypatch):
    router = _reload_router(
        {"ALLOW_DEEPSEEK": "0", "ALLOW_ANTHROPIC": "0", "DEEPSEEK_API_KEY": ""},
        monkeypatch,
    )
    tiers = router._build_tier_order(model_override=None)
    assert [name for name, _ in tiers] == ["local"]


def test_deepseek_added_when_allowed_with_key(monkeypatch):
    router = _reload_router(
        {
            "ALLOW_DEEPSEEK": "1",
            "DEEPSEEK_API_KEY": "sk-test",
            "KOLAW_PREFER_DEEPSEEK": "0",
            "ALLOW_ANTHROPIC": "0",
        },
        monkeypatch,
    )
    tiers = router._build_tier_order(model_override=None)
    assert [name for name, _ in tiers] == ["local", "deepseek"]


def test_prefer_deepseek_flips_order(monkeypatch):
    router = _reload_router(
        {
            "ALLOW_DEEPSEEK": "1",
            "DEEPSEEK_API_KEY": "sk-test",
            "KOLAW_PREFER_DEEPSEEK": "1",
            "ALLOW_ANTHROPIC": "0",
        },
        monkeypatch,
    )
    tiers = router._build_tier_order(model_override=None)
    assert [name for name, _ in tiers] == ["deepseek", "local"]


def test_deepseek_skipped_when_key_missing(monkeypatch):
    router = _reload_router(
        {"ALLOW_DEEPSEEK": "1", "DEEPSEEK_API_KEY": "", "ALLOW_ANTHROPIC": "0"},
        monkeypatch,
    )
    tiers = router._build_tier_order(model_override=None)
    assert [name for name, _ in tiers] == ["local"]


def test_full_three_tier_chain(monkeypatch):
    router = _reload_router(
        {
            "ALLOW_DEEPSEEK": "1",
            "DEEPSEEK_API_KEY": "sk-d",
            "ALLOW_ANTHROPIC": "1",
            "ANTHROPIC_API_KEY": "sk-a",
            "KOLAW_PREFER_DEEPSEEK": "0",
        },
        monkeypatch,
    )
    tiers = router._build_tier_order(model_override=None)
    assert [name for name, _ in tiers] == ["local", "deepseek", "anthropic"]


@pytest.mark.asyncio
async def test_call_deepseek_posts_to_correct_url(monkeypatch):
    router = _reload_router(
        {
            "DEEPSEEK_API_KEY": "sk-test-abc",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
        },
        monkeypatch,
    )
    captured: dict = {}

    async def fake_post(self, url, json, headers, **_kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello from deepseek"}}]},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await router._call_deepseek(
        [{"role": "user", "content": "ping"}], 64, 0.0
    )
    assert result == "hello from deepseek"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-abc"
    assert captured["json"]["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_call_deepseek_raises_without_key(monkeypatch):
    router = _reload_router({"DEEPSEEK_API_KEY": ""}, monkeypatch)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY is empty"):
        await router._call_deepseek([{"role": "user", "content": "x"}], 8, 0.0)
