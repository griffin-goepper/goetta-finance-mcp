"""Regression probe: how does FastMCP route ``_``-prefixed return-dict keys?

Background: the MCP spec uses ``_meta`` as a server-private metadata block
that clients may hide from the model. Some MCP SDKs have historically
treated *any* leading-underscore key in a tool return dict as private. If
FastMCP did that here, our lazy-sync freshness hints (``_stale``,
``_sync_triggered``, etc.) would silently disappear from Claude's view —
the worst kind of bug because it has no error signal.

This probe pins the *observed* contract in the version of FastMCP we
ship against. As of this commit:

    - underscore keys ARE included in ``content`` (the text block Claude
      sees) as plain JSON
    - underscore keys ARE included in ``structuredContent``
    - ``_meta`` is empty (None) on the response

That's the contract our ``sync_status`` MCP tool design depends on. If a
future FastMCP version flips any of these to hide underscore keys, the
assertions below fail loudly and the lazy-sync code needs to switch to
non-underscore field names (``data_age_hours``, ``last_sync_iso``, etc.).

Don't delete this test even after lazy-sync ships — it's the only thing
catching a silent contract drift on a future SDK bump.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session


def _build_probe() -> FastMCP:
    mcp = FastMCP("probe")

    @mcp.tool(description="Returns a dict mixing leading-underscore and plain keys.")
    def mixed() -> dict[str, Any]:
        return {
            "_stale": True,
            "_sync_triggered": False,
            "data_age_hours": 12.5,
            "last_sync_iso": "2026-05-17T06:00:00Z",
            "value": 42,
        }

    return mcp


@pytest.mark.anyio
async def test_underscore_keys_reach_both_content_and_structured() -> None:
    mcp = _build_probe()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("mixed", {})

    structured = getattr(result, "structuredContent", None)
    assert isinstance(structured, dict), f"no structuredContent: {result!r}"
    assert structured.get("_stale") is True, (
        "underscore key dropped from structuredContent — lazy-sync design "
        "relied on this. See tests/test_lazy_sync_visibility.py docstring."
    )
    assert structured.get("_sync_triggered") is False
    assert structured.get("data_age_hours") == 12.5
    assert structured.get("value") == 42

    text_blocks = [b for b in (result.content or []) if getattr(b, "type", None) == "text"]
    assert text_blocks, f"no text content block returned: {result!r}"
    text_payload: dict[str, Any] = {}
    for block in text_blocks:
        raw = getattr(block, "text", None) or ""
        try:
            text_payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        else:
            break
    assert text_payload, "no text block was JSON-parseable"
    assert "_stale" in text_payload, (
        "underscore key was stripped from the model-visible text content. "
        "If this assertion fails, switch lazy-sync freshness fields to "
        "non-underscore names (data_age_hours, last_sync_iso)."
    )
    assert text_payload["_stale"] is True
    assert text_payload["data_age_hours"] == 12.5


@pytest.mark.anyio
async def test_meta_block_is_empty_by_default() -> None:
    """A bare dict return shouldn't populate ``_meta``. If this starts
    failing, FastMCP has started routing underscore keys *to* ``_meta`` —
    that's the failure mode we're guarding against. Lazy-sync field names
    need to switch in that world."""
    mcp = _build_probe()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("mixed", {})
    meta = getattr(result, "_meta", None) or getattr(result, "meta", None)
    assert meta in (None, {}), (
        f"unexpected _meta payload: {meta!r}. FastMCP may have started "
        f"routing underscore-prefix keys to _meta — lazy-sync field names "
        f"need to drop the underscore prefix."
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
