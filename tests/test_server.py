# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Behavioural tests for server.py.

Run with:  pytest tests/test_server.py -v
"""
import base64
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

@pytest.fixture
def clean_env():
    """Reload the server with every opt-in gate closed, whatever the ambient env."""
    gates = ("FITNESSE_FILES_ROOT", "FITNESSE_COMPLETE_TOOLSET", "FITNESSE_ALLOW_SHUTDOWN", "FITNESSE_READONLY")
    previous = {k: os.environ.pop(k, None) for k in gates}
    try:
        importlib.reload(my_server)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)


@pytest.mark.asyncio
async def test_tool_count_full(clean_env):
    async with Client(my_server.mcp) as c:
        tools = await c.list_tools()
    assert len(tools) == 22


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
# Security: files section confinement
# ---------------------------------------------------------------------------

@pytest.fixture
def files_env():
    """Reload the server with every files tool registered.

    FITNESSE_FILES_ROOT names the resource root on the *remote* FitNesse
    instance ("files"); neither download nor upload touches local disk.
    """
    gates = ("FITNESSE_FILES_ROOT", "FITNESSE_COMPLETE_TOOLSET", "FITNESSE_ALLOW_SHUTDOWN", "FITNESSE_READONLY")
    previous = {k: os.environ.pop(k, None) for k in gates}
    os.environ["FITNESSE_FILES_ROOT"] = "files"
    os.environ["FITNESSE_COMPLETE_TOOLSET"] = "1"
    try:
        importlib.reload(my_server)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)


@pytest.mark.parametrize("files_path", ["FrontPage", "", "/", "files/../FrontPage", "notfiles"])
@pytest.mark.asyncio
async def test_files_path_outside_section_rejected(files_env, files_path):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="must stay below"):
            await c.call_tool("fitnesse_list_files", {"files_path": files_path})


@pytest.mark.parametrize(
    "tool,args",
    [
        ("fitnesse_create_dir", {"files_path": "FrontPage", "dirname": "d"}),
        ("fitnesse_delete_file", {"files_path": "FrontPage", "filename": "f.txt"}),
        ("fitnesse_download_file", {"files_path": "FrontPage", "filename": "f.txt"}),
        ("fitnesse_rename_file", {"files_path": "FrontPage", "filename": "f.txt", "new_name": "g.txt"}),
        ("fitnesse_upload_file", {"files_path": "FrontPage", "filename": "f.txt", "content": "x"}),
    ],
)
@pytest.mark.asyncio
async def test_every_files_tool_confined_to_section(files_env, tool, args):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="must stay below"):
            await c.call_tool(tool, args)


@pytest.mark.parametrize("filename", ["../f.txt", "sub/f.txt", "..", "", 'a"b'])
@pytest.mark.asyncio
async def test_filename_must_be_plain_name(files_env, filename):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="plain name without separators"):
            await c.call_tool(
                "fitnesse_delete_file", {"files_path": "files", "filename": filename}
            )


@pytest.mark.asyncio
async def test_files_path_inside_section_allowed(files_env):
    with _patch_urlopen():
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_list_files", {"files_path": "/files/images/"})
    assert result.data["ok"] is True
    assert result.data["url"].endswith("/files/images?responder=files&format=json")


@pytest.mark.asyncio
async def test_files_root_is_configurable_per_instance(files_env):
    """FITNESSE_FILES_ROOT names the root segment on THIS FitNesse instance,
    which can differ from the FitNesse default of 'files'."""
    os.environ["FITNESSE_FILES_ROOT"] = "assets"
    importlib.reload(my_server)
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="must stay below 'assets/'"):
            await c.call_tool("fitnesse_list_files", {"files_path": "files/images"})
        with _patch_urlopen():
            result = await c.call_tool("fitnesse_list_files", {"files_path": "assets/images"})
    assert result.data["ok"] is True


@pytest.mark.parametrize(
    "tool",
    ["fitnesse_list_files", "fitnesse_create_dir", "fitnesse_upload_file", "fitnesse_download_file", "fitnesse_delete_file", "fitnesse_rename_file"],
)
@pytest.mark.asyncio
async def test_files_tools_hidden_without_files_root(tool):
    previous = os.environ.pop("FITNESSE_FILES_ROOT", None)
    try:
        importlib.reload(my_server)
        async with Client(my_server.mcp) as c:
            tools = {t.name for t in await c.list_tools()}
        assert tool not in tools
        assert "fitnesse_delete_page" in tools
    finally:
        if previous is not None:
            os.environ["FITNESSE_FILES_ROOT"] = previous
        importlib.reload(my_server)


@pytest.mark.asyncio
async def test_rename_file_needs_only_files_root():
    """fitnesse_rename_file must not require FITNESSE_COMPLETE_TOOLSET."""
    gates = ("FITNESSE_FILES_ROOT", "FITNESSE_COMPLETE_TOOLSET")
    previous = {k: os.environ.pop(k, None) for k in gates}
    os.environ["FITNESSE_FILES_ROOT"] = "files"
    try:
        importlib.reload(my_server)
        async with Client(my_server.mcp) as c:
            tools = {t.name for t in await c.list_tools()}
        assert "fitnesse_rename_file" in tools
        assert "fitnesse_get_page" not in tools  # a genuine FITNESSE_COMPLETE_TOOLSET tool
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)


@pytest.mark.asyncio
async def test_download_html_file_returns_text_content(files_env):
    html = b"<html><body>hi</body></html>"
    with _patch_urlopen(body=html, content_type="text/html"):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool(
                "fitnesse_download_file",
                {"files_path": "files", "filename": "page.html"},
            )
    assert result.data["ok"] is True
    assert result.data["content_type"] == "text/html"
    assert result.data["bytes"] == len(html)
    [block] = result.content
    assert block.resource.text == html.decode()


@pytest.mark.asyncio
async def test_download_image_file_returns_image_content(files_env):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    with _patch_urlopen(body=png, content_type="image/png"):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool(
                "fitnesse_download_file",
                {"files_path": "files", "filename": "shot.png"},
            )
    assert result.data["ok"] is True
    [block] = result.content
    assert block.type == "image"
    assert block.mime_type == "image/png"
    assert base64.b64decode(block.data) == png


@pytest.mark.asyncio
async def test_download_file_exceeding_transfer_limit_rejected(files_env, monkeypatch):
    monkeypatch.setattr(my_server, "MAX_TRANSFER_BYTES", 4)
    with _patch_urlopen(body=b"toolong", content_type="text/plain"):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool(
                "fitnesse_download_file",
                {"files_path": "files", "filename": "big.txt"},
            )
    assert result.data["ok"] is False
    assert "transfer limit" in result.data["error"]


@pytest.mark.asyncio
async def test_upload_file_with_text_content(files_env):
    with _patch_urlopen():
        async with Client(my_server.mcp) as c:
            result = await c.call_tool(
                "fitnesse_upload_file",
                {"files_path": "files", "filename": "note.html", "content": "<p>hi</p>"},
            )
    assert result.data["ok"] is True


@pytest.mark.asyncio
async def test_upload_file_with_base64_content(files_env):
    payload = b"\x89PNG\r\n\x1a\n"
    with _patch_urlopen():
        async with Client(my_server.mcp) as c:
            result = await c.call_tool(
                "fitnesse_upload_file",
                {
                    "files_path": "files",
                    "filename": "shot.png",
                    "content_base64": base64.b64encode(payload).decode(),
                },
            )
    assert result.data["ok"] is True


@pytest.mark.asyncio
async def test_upload_file_requires_exactly_one_content_source(files_env):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="exactly one of content or content_base64"):
            await c.call_tool(
                "fitnesse_upload_file", {"files_path": "files", "filename": "note.txt"}
            )
        with pytest.raises(ToolError, match="exactly one of content or content_base64"):
            await c.call_tool(
                "fitnesse_upload_file",
                {
                    "files_path": "files",
                    "filename": "note.txt",
                    "content": "a",
                    "content_base64": "YQ==",
                },
            )


@pytest.mark.asyncio
async def test_upload_file_rejects_invalid_base64(files_env):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="not valid base64"):
            await c.call_tool(
                "fitnesse_upload_file",
                {"files_path": "files", "filename": "note.txt", "content_base64": "not-valid-base64!!"},
            )


@pytest.mark.asyncio
async def test_upload_file_exceeding_transfer_limit_rejected(files_env, monkeypatch):
    monkeypatch.setattr(my_server, "MAX_TRANSFER_BYTES", 4)
    async with Client(my_server.mcp) as c:
        result = await c.call_tool(
            "fitnesse_upload_file",
            {"files_path": "files", "filename": "big.txt", "content": "toolong"},
        )
    assert result.data["ok"] is False
    assert "limit is 4" in result.data["error"]


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


# ---------------------------------------------------------------------------
# Complete toolset gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_toolset_hidden_by_default():
    async with Client(my_server.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
    assert "fitnesse_get_page" not in tools
    assert "fitnesse_get_raw" not in tools


@pytest.mark.asyncio
async def test_complete_toolset_exposed_when_flagged(clean_env):
    os.environ["FITNESSE_COMPLETE_TOOLSET"] = "1"
    importlib.reload(my_server)
    async with Client(my_server.mcp) as c:
        tools = await c.list_tools()
    names = {t.name for t in tools}
    assert "fitnesse_get_page" in names
    assert "fitnesse_get_raw" in names
    assert "fitnesse_publish" in names
    assert len(tools) == 37


@pytest.mark.asyncio
async def test_files_root_adds_files_tools(files_env):
    """FITNESSE_FILES_ROOT plus the complete toolset exposes all six files tools."""
    async with Client(my_server.mcp) as c:
        tools = await c.list_tools()
    names = {t.name for t in tools}
    assert {
        "fitnesse_list_files",
        "fitnesse_download_file",
        "fitnesse_upload_file",
        "fitnesse_delete_file",
        "fitnesse_rename_file",
        "fitnesse_create_dir",
    } <= names
    assert len(tools) == 43
