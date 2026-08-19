# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Behavioural tests for server.py.

Run with:  pytest tests/test_server.py -v
"""
import importlib
import os
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server as my_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal urllib response stub."""
    status = 200

    def __init__(self, body: bytes = b'{"ok":true}', content_type: str = "application/json"):
        self._body = body
        self._ct = content_type

    @property
    def headers(self):
        ct = self._ct
        return type("H", (), {"get": lambda self, k, d="": ct})()

    def read(self, n: int) -> bytes:
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _patch_urlopen(body: bytes = b'{"ok":true}', content_type: str = "application/json"):
    return patch("urllib.request.urlopen", return_value=_FakeResponse(body, content_type))


# ---------------------------------------------------------------------------
# Tool list and schema
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_count_full():
    async with Client(my_server.mcp) as c:
        tools = await c.list_tools()
    assert len(tools) == 31


@pytest.mark.asyncio
async def test_timeout_not_in_schema():
    async with Client(my_server.mcp) as c:
        tools = await c.list_tools()
    exposed = [t for t in tools if "timeout_seconds" in (getattr(t, "input_schema", {}).get("properties") or {})]
    assert exposed == [], f"timeout_seconds exposed on: {[t.name for t in exposed]}"


@pytest.mark.asyncio
async def test_page_path_description_in_schema():
    async with Client(my_server.mcp) as c:
        tools = await c.list_tools()
    get_page = next(t for t in tools if t.name == "fitnesse_get_page_data")
    schema = getattr(get_page, "input_schema", {})
    desc = schema["properties"]["page_path"]["description"]
    assert "FrontPage" in desc
    assert "No leading slash" in desc


# ---------------------------------------------------------------------------
# Read tool end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_tool_returns_ok():
    with _patch_urlopen():
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_data", {"page_path": "FrontPage"})
    assert result.data["ok"] is True
    assert "FrontPage" in result.data["url"]


@pytest.mark.asyncio
async def test_timeout_resolved_to_float():
    """Depends injection must resolve timeout to a float before urlopen is called."""
    captured = {}

    def spy(*_, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=spy):
        async with Client(my_server.mcp) as c:
            await c.call_tool("fitnesse_get_page_data", {"page_path": "FrontPage"})

    assert isinstance(captured["timeout"], float), f"expected float, got {type(captured['timeout'])}"
    assert captured["timeout"] == 30.0


# ---------------------------------------------------------------------------
# Security: path validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_injection_rejected():
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="Invalid path"):
            await c.call_tool("fitnesse_get_page_data", {"page_path": "FrontPage?responder=deletePage"})


@pytest.mark.asyncio
async def test_path_traversal_rejected():
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="Invalid path"):
            await c.call_tool("fitnesse_get_page_data", {"page_path": "../../etc/passwd"})


@pytest.mark.asyncio
async def test_reserved_param_injection_rejected():
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="Reserved query parameters"):
            await c.call_tool(
                "fitnesse_run_test",
                {"page_path": "FrontPage", "variables": {"responder": "deletePage"}},
            )


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_readonly_hides_write_tools():
    os.environ["FITNESSE_READONLY"] = "1"
    try:
        importlib.reload(my_server)
        async with Client(my_server.mcp) as c:
            tools = {t.name for t in await c.list_tools()}
        assert "fitnesse_delete_page" not in tools
        assert "fitnesse_run_suite" not in tools
        assert "fitnesse_get_page_data" in tools
    finally:
        del os.environ["FITNESSE_READONLY"]
        importlib.reload(my_server)


@pytest.mark.asyncio
async def test_readonly_false_string_does_not_enable():
    os.environ["FITNESSE_READONLY"] = "false"
    try:
        importlib.reload(my_server)
        async with Client(my_server.mcp) as c:
            tools = {t.name for t in await c.list_tools()}
        assert "fitnesse_delete_page" in tools
    finally:
        del os.environ["FITNESSE_READONLY"]
        importlib.reload(my_server)


# ---------------------------------------------------------------------------
# Shutdown gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shutdown_hidden_by_default():
    async with Client(my_server.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
    assert "fitnesse_shutdown" not in tools


@pytest.mark.asyncio
async def test_shutdown_exposed_when_flagged():
    os.environ["FITNESSE_ALLOW_SHUTDOWN"] = "1"
    try:
        importlib.reload(my_server)
        async with Client(my_server.mcp) as c:
            tools = {t.name for t in await c.list_tools()}
        assert "fitnesse_shutdown" in tools
    finally:
        del os.environ["FITNESSE_ALLOW_SHUTDOWN"]
        importlib.reload(my_server)
