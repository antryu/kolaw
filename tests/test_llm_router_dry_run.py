"""
test_llm_router_dry_run.py — router with dry_run=True returns deterministic mock.
"""

import asyncio

import pytest

from services.llm.router import _dry_run_response, complete


def test_dry_run_returns_string():
    messages = [{"role": "user", "content": "ping"}]
    result = _dry_run_response(messages)
    assert isinstance(result, str)
    assert "dry_run" in result


def test_dry_run_deterministic():
    messages = [{"role": "user", "content": "deterministic test"}]
    result1 = _dry_run_response(messages)
    result2 = _dry_run_response(messages)
    assert result1 == result2


def test_dry_run_different_inputs_differ():
    r1 = _dry_run_response([{"role": "user", "content": "query A"}])
    r2 = _dry_run_response([{"role": "user", "content": "query B"}])
    assert r1 != r2


@pytest.mark.asyncio
async def test_complete_dry_run():
    messages = [{"role": "user", "content": "what is the capital of Korea?"}]
    result = await complete(messages, dry_run=True)
    assert isinstance(result, str)
    assert len(result) > 0
    assert "dry_run" in result


@pytest.mark.asyncio
async def test_complete_dry_run_no_network_call():
    """dry_run must not make any network calls — runs fast."""
    import time

    messages = [{"role": "user", "content": "test"}]
    start = time.time()
    await complete(messages, dry_run=True)
    elapsed = time.time() - start
    assert elapsed < 0.5, f"dry_run took {elapsed:.2f}s — suspected network call"
