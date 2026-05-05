"""
Tests for the law.go.kr direct API client (Phase 3).

These hit the live law.go.kr Open API. They require LAW_GO_KR_OC to be set
(otherwise skipped). Marked as integration-flavored — they hit the network,
so they're skipped if RUN_INTEGRATION isn't set OR if OC is missing.
"""

from __future__ import annotations

import os

import pytest

from services.data.law_go_kr import (
    LawGoKrClient,
    LawGoKrItem,
    LawGoKrUnavailable,
)

_OC = os.getenv("LAW_GO_KR_OC", "")
_RUN_INTEGRATION = os.getenv("RUN_INTEGRATION") == "1"

requires_live = pytest.mark.skipif(
    not _OC or not _RUN_INTEGRATION,
    reason="LAW_GO_KR_OC not set or RUN_INTEGRATION!=1 — skipping live API tests",
)


# ─── Pure / unit tests (no network) ───────────────────────────────────────


def test_client_without_oc_raises():
    """Unconfigured client must explain how to get an OC, not silently no-op."""
    import asyncio

    c = LawGoKrClient(oc="")
    ok, msg = asyncio.get_event_loop().run_until_complete(c.is_available())
    assert ok is False
    assert "LAW_GO_KR_OC" in msg or "configured" in msg.lower()


def test_law_go_kr_item_dataclass_defaults():
    item = LawGoKrItem(target="law", item_id="123", title="t")
    assert item.subtitle == ""
    assert item.enforced_at == ""
    assert item.detail_url == ""
    assert item.raw == {}


# ─── Live integration tests ───────────────────────────────────────────────


@requires_live
@pytest.mark.asyncio
async def test_law_go_kr_health_ok():
    c = LawGoKrClient()
    ok, msg = await c.is_available()
    assert ok is True
    assert _OC in msg


@requires_live
@pytest.mark.asyncio
async def test_law_go_kr_search_law_returns_items():
    c = LawGoKrClient()
    items = await c.search_law("의료법", display=3)
    assert len(items) >= 1
    first = items[0]
    assert first.target == "law"
    assert first.title
    assert first.item_id
    assert first.detail_url.startswith("https://www.law.go.kr/")


@requires_live
@pytest.mark.asyncio
async def test_law_go_kr_precedent_returns_items():
    c = LawGoKrClient()
    items = await c.search_precedent("부당해고", display=3)
    assert len(items) >= 1
    first = items[0]
    assert first.target == "prec"
    assert "부당해고" in first.title or first.title  # any non-empty title is OK


@requires_live
@pytest.mark.asyncio
async def test_law_go_kr_interpretation_returns_items():
    c = LawGoKrClient()
    items = await c.search_interpretation("의료", display=2)
    assert len(items) >= 1
    assert items[0].target == "expc"


@requires_live
@pytest.mark.asyncio
async def test_law_go_kr_admin_rule_returns_items():
    c = LawGoKrClient()
    items = await c.search_admin_rule("의료", display=2)
    assert len(items) >= 1
    assert items[0].target == "admrul"


@requires_live
@pytest.mark.asyncio
async def test_law_go_kr_ordinance_uses_overridden_list_key():
    """Ordinance responses nest rows under 'law' (not 'ordin') — verify normalization."""
    c = LawGoKrClient()
    items = await c.search_ordinance("의료", display=2)
    assert len(items) >= 1
    assert items[0].target == "ordin"
    assert items[0].title  # non-empty title proves field mapping worked
