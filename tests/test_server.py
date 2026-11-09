# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Behavioural tests for server.py.

Run with:  pytest tests/test_server.py -v
"""
import base64
import json
import importlib
import os
from unittest.mock import patch
from urllib.error import HTTPError

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

    def __init__(self, body: bytes = b'{"ok":true}', content_type: str = "application/json", chunk: int | None = None):
        self._body = body
        self._ct = content_type
        self._chunk = chunk  # at most this many bytes per read, as http.client may return

    @property
    def headers(self):
        ct = self._ct
        return type("H", (), {"get": lambda self, k, d="": ct})()

    def read(self, n: int) -> bytes:
        n = min(n, self._chunk or n)
        data, self._body = self._body[:n], self._body[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _patch_urlopen(body: bytes = b'{"ok":true}', content_type: str = "application/json"):
    """Every request gets a fresh response with this body."""
    return patch("urllib.request.urlopen", side_effect=lambda *a, **k: _FakeResponse(body, content_type))


# ---------------------------------------------------------------------------
# Tool list and schema
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env():
    """Reload the server with every opt-in gate closed, whatever the ambient env."""
    gates = ("FITNESSE_FILES_ROOT", "FITNESSE_ALLOW_SHUTDOWN", "FITNESSE_TOOLSETS")
    previous = {k: os.environ.pop(k, None) for k in gates}
    os.environ["FITNESSE_TOOLSETS"] = "all"
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
    assert len(tools) == 27
    assert "fitnesse_search" in {t.name for t in tools}


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
async def test_body_read_to_the_end_when_reads_come_short():
    body = b'{"ok": true, "items": [' + b",".join(b'"%d"' % i for i in range(200)) + b"]}"
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _FakeResponse(body, chunk=7)):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_data", {"page_path": "FrontPage"})
    body_out = result.data["body"]
    assert (json.loads(body_out) if isinstance(body_out, str) else body_out)["items"][-1] == "199"
    assert result.data["truncated"] is False


class _CutResponse(_FakeResponse):
    """Announces the whole body in Content-Length but ends after `cut` bytes."""

    def __init__(self, body: bytes, cut: int):
        super().__init__(body[:cut])
        self._length = str(len(body))

    @property
    def headers(self):
        length, ct = self._length, self._ct
        return type("H", (), {"get": lambda self, k, d="": length if k == "Content-Length" else ct})()


@pytest.mark.asyncio
async def test_answer_that_ends_early_is_fetched_again_for_get_only():
    body = b'{"ok": true, "pad": "' + b"x" * 300 + b'"}'
    answers = iter([_CutResponse(body, 50), _FakeResponse(body)])
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(answers)) as urlopen:
        result = my_server._request("GET", "FrontPage")
    assert result["ok"] is True and result["body"]["ok"] is True and urlopen.call_count == 2

    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _CutResponse(body, 50)) as urlopen:
        result = my_server._request("GET", "FrontPage")
    assert result["ok"] is False and urlopen.call_count == 3
    assert result["error"] == f"FitNesse closed the connection after 50 of {len(body)} bytes; tried 3 times"

    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _CutResponse(body, 50)) as urlopen:
        result = my_server._request("POST", "FrontPage", body="x")
    assert result["ok"] is False and urlopen.call_count == 1 and "may have taken effect" in result["error"]


def test_get_that_runs_tests_is_never_repeated():
    body = b'{"ok": true, "pad": "' + b"x" * 300 + b'"}'
    for responder in ("suite", "test", "deletePage", "purgeHistory"):
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _CutResponse(body, 50)) as urlopen:
            result = my_server._request("GET", "S", params={"responder": responder, "format": "xml"})
        assert urlopen.call_count == 1, responder
        assert "may have taken effect" in result["error"] and "fitnesse_get_page_history" in result["error"]
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _CutResponse(body, 50)) as urlopen:
        my_server._request("GET", "S", [("responder", "names"), ("format", "json")])
    assert urlopen.call_count == 3  # params as a list of pairs, a responder that only reads


def test_connection_closed_before_the_status_line():
    import http.client
    with patch("urllib.request.urlopen", side_effect=http.client.BadStatusLine("")) as urlopen:
        result = my_server._request("GET", "S", params={"responder": "suite"})
    assert urlopen.call_count == 1 and result["ok"] is False
    assert result["error"].startswith("FitNesse closed the connection without a complete answer (BadStatusLine)")
    with patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset")) as urlopen:
        result = my_server._request_bytes("GET", "S", params={"responder": "pageHistory"})
    assert urlopen.call_count == 3 and "(ConnectionResetError: reset); tried 3 times" in result["error"]


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


@pytest.mark.parametrize("tool", ["fitnesse_run_test", "fitnesse_run_suite"])
@pytest.mark.parametrize("configured", [120.0, 14400.0, None])
@pytest.mark.asyncio
async def test_run_timeout_configurable(tool, configured):
    """FITNESSE_RUN_TIMEOUT_SECONDS sets the run tools' timeout; None disables it."""
    captured = {}

    def spy(*_, timeout="unset"):
        captured["timeout"] = timeout
        return _FakeResponse()

    with (
        patch.object(my_server, "RUN_TIMEOUT_SECONDS", configured),
        patch("urllib.request.urlopen", side_effect=spy),
    ):
        async with Client(my_server.mcp) as c:
            await c.call_tool(tool, {"page_path": "FrontPage"})

    assert captured["timeout"] == configured


@pytest.mark.parametrize("tool", ["fitnesse_run_test", "fitnesse_run_suite"])
@pytest.mark.asyncio
async def test_run_flags_forwarded(tool):
    captured = {}

    def spy(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=spy):
        async with Client(my_server.mcp) as c:
            await c.call_tool(
                tool,
                {"page_path": "FrontPage", "debug": True, "remote_debug": True, "nochunk": True},
            )

    query = captured["url"].split("?", 1)[1].split("&")
    assert {"debug", "remote_debug", "nochunk"} <= set(query)


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
    gates = ("FITNESSE_FILES_ROOT", "FITNESSE_ALLOW_SHUTDOWN", "FITNESSE_TOOLSETS")
    previous = {k: os.environ.pop(k, None) for k in gates}
    os.environ["FITNESSE_FILES_ROOT"] = "files"
    os.environ["FITNESSE_TOOLSETS"] = "all,complete"
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
    """fitnesse_rename_file must not require the complete toolset."""
    gates = ("FITNESSE_FILES_ROOT", "FITNESSE_TOOLSETS")
    previous = {k: os.environ.pop(k, None) for k in gates}
    os.environ["FITNESSE_FILES_ROOT"] = "files"
    os.environ["FITNESSE_TOOLSETS"] = "all"
    try:
        importlib.reload(my_server)
        async with Client(my_server.mcp) as c:
            tools = {t.name for t in await c.list_tools()}
        assert "fitnesse_rename_file" in tools
        assert "fitnesse_get_page" not in tools  # a genuine complete-toolset tool
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
# FITNESSE_TOOLSETS
# ---------------------------------------------------------------------------

_TOOLSET_GATES = (
    "FITNESSE_TOOLSETS", "FITNESSE_FILES_ROOT",
    "FITNESSE_ALLOW_SHUTDOWN", "FITNESSE_ALLOW_ACCOUNT_ADMIN", "FITNESSE_ALLOW_ADOPT",
)
_OTHER_GATES_OPEN = {
    "FITNESSE_FILES_ROOT": "files", "FITNESSE_ALLOW_SHUTDOWN": "1",
    "FITNESSE_ALLOW_ACCOUNT_ADMIN": "1",
}


@pytest.fixture
def toolsets_env():
    """Reload the server with every other gate open and the given FITNESSE_TOOLSETS."""
    previous = {k: os.environ.pop(k, None) for k in _TOOLSET_GATES}

    async def load(toolsets: str | None) -> set[str]:
        for key in _TOOLSET_GATES:
            os.environ.pop(key, None)
        os.environ.update(_OTHER_GATES_OPEN)
        if toolsets is not None:
            os.environ["FITNESSE_TOOLSETS"] = toolsets
        importlib.reload(my_server)
        async with Client(my_server.mcp) as c:
            return {t.name for t in await c.list_tools()}

    try:
        yield load
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)


def _modes_of(tool) -> set[str]:
    return {
        mode
        for mode in my_server.MODES
        if tool.tags & my_server.MODE_TAGS[mode] or tool.name in my_server.MODE_NAMES[mode]
    }


@pytest.mark.asyncio
async def test_every_tool_needs_exactly_one_mode(toolsets_env):
    await toolsets_env("all,debug,complete")
    tools = await my_server.mcp.list_tools()
    assert len(tools) == 68
    assert {t.name: _modes_of(t) for t in tools if len(_modes_of(t)) != 1} == {}
    debug = {t.name: _modes_of(t).pop() for t in tools if "debug" in t.tags}
    assert debug == {
        "fitnesse_debug_status": "read", "fitnesse_debug_page_tables": "read", "fitnesse_debug_read_link": "read", "fitnesse_debug_steps": "read",
        "fitnesse_debug_inspect": "read", "fitnesse_table_source": "read",
        "fitnesse_debug_defined_action_source": "read",
        "fitnesse_table_save": "write", "fitnesse_table_insert": "write", "fitnesse_table_delete": "write",
        "fitnesse_table_move": "write", "fitnesse_table_save_text": "write", "fitnesse_table_move_text": "write",
        "fitnesse_debug_start": "test", "fitnesse_debug_run_wiki": "test",
        "fitnesse_debug_run_page": "test", "fitnesse_debug_end": "test",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "toolsets",
    ["read", "write", "test", "read,test", "read,write", "write,test", "read,debug", "test,debug",
     "write,debug", "read,complete", "read,test,debug,complete", "read,write,test"],
)
async def test_toolsets_show_exactly_the_matching_tools(toolsets_env, toolsets):
    everything = await toolsets_env("all,debug,complete")
    tools = {t.name: t for t in await my_server.mcp.list_tools()}
    complete_only = everything - await toolsets_env("read,write,test,debug")
    visible = await toolsets_env(toolsets)
    chosen = set(toolsets.split(","))
    expected = {
        name for name in everything
        if _modes_of(tools[name]) <= chosen
        and ("debug" in chosen or "debug" not in tools[name].tags)
        and ("complete" in chosen or name not in complete_only)
    }
    assert visible == expected


@pytest.mark.asyncio
async def test_toolsets_examples(toolsets_env):
    read = await toolsets_env("read")
    assert {"fitnesse_get_page_data", "fitnesse_search"} <= read
    assert not {"fitnesse_save_page_content", "fitnesse_run_suite", "fitnesse_stop_test"} & read
    assert not any("debug" in n or "table" in n for n in read)
    assert "fitnesse_get_raw" not in read
    assert "fitnesse_get_raw" in await toolsets_env("read,complete")
    debug_read = await toolsets_env("read,debug")
    assert {"fitnesse_debug_status", "fitnesse_debug_inspect", "fitnesse_table_source"} <= debug_read
    assert not {"fitnesse_debug_start", "fitnesse_table_save", "fitnesse_run_test"} & debug_read
    debug_test = await toolsets_env("test,debug")
    assert {"fitnesse_debug_start", "fitnesse_debug_run_wiki", "fitnesse_run_suite"} <= debug_test
    assert not {"fitnesse_debug_status", "fitnesse_table_source"} & debug_test
    write = await toolsets_env("read,write")
    assert {"fitnesse_shutdown", "fitnesse_save_account"} <= write
    assert not {"fitnesse_table_save", "fitnesse_run_test"} & write
    assert "fitnesse_table_save" in await toolsets_env("write,debug")


@pytest.mark.asyncio
async def test_toolsets_default_all_and_spelling(toolsets_env):
    default = await toolsets_env("read,write,test,debug")
    assert default == await toolsets_env(" Read , WRITE,test,DEBUG ")
    assert len(default) == 52  # 68 minus the 16 complete tools
    assert await toolsets_env("all,debug") == default  # all leaves the areas out
    assert await toolsets_env("read,all,debug") == default
    assert len(await toolsets_env("all")) == 35  # minus 16 complete and 17 debug tools
    assert len(await toolsets_env("all,debug,complete")) == 68
    assert len(await toolsets_env("read,write,test,debug,complete")) == 68
    assert len(await toolsets_env("read,write,test,complete")) == 51  # minus 17 debug tools


@pytest.mark.parametrize("value", [None, "", "  "])
def test_toolsets_required(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FITNESSE_TOOLSETS", raising=False)
    else:
        monkeypatch.setenv("FITNESSE_TOOLSETS", value)
    with pytest.raises(RuntimeError, match="FITNESSE_TOOLSETS is required"):
        my_server._toolsets_from_env()


@pytest.mark.parametrize("value", ["readonly", "read,admin", ",", "read;write", "everything"])
def test_toolsets_reject_unknown_values(monkeypatch, value):
    monkeypatch.setenv("FITNESSE_TOOLSETS", value)
    with pytest.raises(RuntimeError, match="FITNESSE_TOOLSETS="):
        my_server._toolsets_from_env()


@pytest.mark.parametrize("value", ["debug", "complete", "debug,complete"])
def test_toolsets_reject_area_without_mode(monkeypatch, value):
    monkeypatch.setenv("FITNESSE_TOOLSETS", value)
    with pytest.raises(RuntimeError, match="need at least one of read, write, test"):
        my_server._toolsets_from_env()


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
    os.environ["FITNESSE_TOOLSETS"] = "all,complete"
    importlib.reload(my_server)
    async with Client(my_server.mcp) as c:
        tools = await c.list_tools()
    names = {t.name for t in tools}
    assert "fitnesse_get_page" in names
    assert "fitnesse_get_raw" in names
    assert "fitnesse_publish" in names
    assert len(tools) == 43


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
    assert len(tools) == 49


# ---------------------------------------------------------------------------
# Page history executionLog filtering
# ---------------------------------------------------------------------------

_HISTORY_XML = (
    b"<?xml version='1.0' encoding='utf-8'?><suiteResults><executionLog>"
    b"<testSystem>slim</testSystem><command>java -jar x</command><exitCode>0</exitCode>"
    b"<stdOut>lots of output</stdOut><stdErr>warn</stdErr></executionLog>"
    b"<finalCounts><right>1</right></finalCounts></suiteResults>"
)


@pytest.mark.asyncio
async def test_page_history_strips_execution_log_by_default():
    with _patch_urlopen(body=_HISTORY_XML, content_type="text/xml"):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_history", {"page_path": "SuiteA", "raw": True})
    body = result.data["body"]
    assert "<testSystem>slim</testSystem>" in body
    assert "<exitCode>0</exitCode>" in body
    assert "<finalCounts>" in body
    for tag in ("command", "stdOut", "stdErr"):
        assert f"<{tag}>" not in body


@pytest.mark.asyncio
async def test_page_history_include_flags_keep_elements():
    with _patch_urlopen(body=_HISTORY_XML, content_type="text/xml"):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool(
                "fitnesse_get_page_history",
                {"page_path": "SuiteA", "include_stdout": True, "include_stderr": True, "raw": True},
            )
    body = result.data["body"]
    assert "<stdOut>lots of output</stdOut>" in body
    assert "<stdErr>warn</stdErr>" in body
    assert "<command>" not in body


@pytest.mark.asyncio
async def test_page_history_non_xml_passed_through(monkeypatch):
    monkeypatch.setattr(my_server, "MAX_BYTES", 5)
    with _patch_urlopen(body=b"<html>not xml", content_type="text/html"):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_history", {"page_path": "SuiteA"})
    assert result.data["body"] == "<html"
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_page_history_logs_when_truncated(monkeypatch, caplog):
    monkeypatch.setattr(my_server, "MAX_BYTES", 20)
    with _patch_urlopen(body=_HISTORY_XML, content_type="text/xml"):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_history", {"page_path": "SuiteA", "raw": True})
    assert result.data["truncated"] is True
    assert "FITNESSE_MAX_RESPONSE_BYTES=20" in caplog.text


_RUNS_XML = (
    b"<?xml version='1.0'?><PageHistory><Page><Name>S.T</Name>"
    b"<TestResult><Date>Fri Nov 06 22:59:35 GMT 2026</Date>"
    b"<ResultLink>S.T?pageHistory&amp;resultDate=20261106225935&amp;format=xml</ResultLink>"
    b"<Pass>3</Pass><Fail>1</Fail></TestResult></Page></PageHistory>"
)
_SUITE_RUN_XML = (
    b"<?xml version='1.0'?><suiteResults><rootPath>S</rootPath><date>2026-11-06T22:59:35Z</date>"
    b"<pageHistoryReference><name>S.A</name><pageHistoryLink>S.A?pageHistory&amp;resultDate=20261106225946</pageHistoryLink>"
    b"<counts><right>9</right><wrong>3</wrong><ignores>0</ignores><exceptions>2</exceptions></counts>"
    b"<runTimeInMillis>16011</runTimeInMillis></pageHistoryReference>"
    b"<pageHistoryReference><name>S.B</name><pageHistoryLink>S.B?pageHistory&amp;resultDate=20261106230022</pageHistoryLink>"
    b"<counts><right>11</right><wrong>0</wrong><ignores>0</ignores><exceptions>0</exceptions></counts>"
    b"<runTimeInMillis>4061</runTimeInMillis></pageHistoryReference>"
    b"<executionLog><command>java</command><exitCode>1</exitCode><stdOut>out</stdOut><stdErr></stdErr></executionLog>"
    b"<finalCounts><right>1</right><wrong>1</wrong><ignores>0</ignores><exceptions>0</exceptions></finalCounts></suiteResults>"
)
_TEST_RUN_XML = (
    b"<?xml version='1.0'?><testResults><rootPath>S.A</rootPath><result>"
    b"<counts><right>1</right><wrong>1</wrong><ignores>0</ignores><exceptions>0</exceptions></counts>"
    b"<date>2026-11-06T23:00:26Z</date><runTimeInMillis>22914</runTimeInMillis><content>"
    b"&lt;table&gt;&lt;tr&gt;&lt;td class=\"pass\"&gt;open&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"
    b"&lt;table&gt;&lt;tr&gt;&lt;td&gt;url&lt;/td&gt;&lt;td class=\"fail\"&gt;x expected&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"
    b"</content><relativePageName>A</relativePageName></result>"
    b"<executionLog><exitCode>0</exitCode></executionLog></testResults>"
)


async def _history(body, **args):
    with _patch_urlopen(body=body, content_type="text/xml"):
        async with Client(my_server.mcp) as c:
            return (await c.call_tool("fitnesse_get_page_history", {"page_path": "S", **args})).data


@pytest.mark.asyncio
async def test_page_history_condensed_by_default():
    data = await _history(_RUNS_XML)
    assert "body" not in data
    assert data["runs"] == [{"page": "S.T", "date": "Fri Nov 06 22:59:35 GMT 2026",
                             "result_date": "20261106225935", "pass": 3, "fail": 1}]

    data = await _history(_SUITE_RUN_XML, result_date="latest")
    assert data["failed_pages"] == ["S.A"] and data["exit_code"] == 1
    assert data["pages"][0] == {"page": "S.A", "outcome": "error", "result_date": "20261106225946",
                                "counts": {"right": 9, "wrong": 3, "ignores": 0, "exceptions": 2}, "run_time_ms": 16011}
    assert data["pages"][1]["outcome"] == "pass" and data["final_counts"]["wrong"] == 1
    assert "stdout" not in data and "command" not in data  # dropped unless asked for
    assert (await _history(_SUITE_RUN_XML, include_stdout=True))["stdout"] == "out"
    secret = _SUITE_RUN_XML.replace(b"<stdOut>out</stdOut>", b"<stdOut>Authorization=Bearer abc\nAccept=*/*</stdOut>")
    assert (await _history(secret, include_stdout=True))["stdout"] == "Authorization=Bearer ***\nAccept=*/*"

    data = await _history(_TEST_RUN_XML, result_date="20261106230026")
    assert data["page"] == "S.A" and data["outcome"] == "fail" and data["run_time_ms"] == 22914
    assert data["tables"] == "Table 1:\n| url | [fail] x expected |"  # only failing tables, page numbers
    assert data["tables_without_failures"] == 1 and "exit_code" not in data


_OVERVIEW_HTML = (
    '<body class="suiteOverview"><table><tr><td class="passed_tests"/><td>1 </td><td>(50%)</td>'
    "<td> Test(s) Passed </td></tr><tr><td class=\"unrun_tests\"/><td>0</td><td>(0%)</td><td> Test(s) Not Run </td></tr>"
    '<tr><td class="failed_tests"/><td>  1 </td><td> (50%)</td><td>Test(s) Failed </td></tr></table><ul>'
    '<li class="completion_test"><a href="S.A?pageHistory&resultDate=20261106225946" class="fail">A</a></li>'
    '<li class="completion_test"><a href="S.B?pageHistory&amp;resultDate=20261106230022" class="done">B</a></li>'
    "</ul></body>"
).encode()


@pytest.mark.asyncio
async def test_suite_overview_condensed_by_default():
    with _patch_urlopen(body=_OVERVIEW_HTML, content_type="text/html"):
        async with Client(my_server.mcp) as c:
            data = (await c.call_tool("fitnesse_get_suite_overview", {"page_path": "S"})).data
            raw = (await c.call_tool("fitnesse_get_suite_overview", {"page_path": "S", "raw": True})).data
    assert data["summary"] == {"passed": 1, "not_run": 0, "failed": 1} and "body" not in data
    assert data["failed_pages"] == ["S.A"]
    assert data["pages"][1] == {"page": "S.B", "outcome": "pass", "result_date": "20261106230022"}
    assert "suiteOverview" in raw["body"]


@pytest.mark.asyncio
async def test_read_tool_logs_when_truncated(monkeypatch, caplog):
    monkeypatch.setattr(my_server, "MAX_BYTES", 4)
    with _patch_urlopen():
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_data", {"page_path": "FrontPage"})
    assert result.data["truncated"] is True
    assert "truncated to FITNESSE_MAX_RESPONSE_BYTES=4" in caplog.text


# ---------------------------------------------------------------------------
# saveData / saveProperties keep what FitNesse would otherwise remove
# ---------------------------------------------------------------------------

_PROPS = (
    b'{"Help": "Old help", "Suites": ["smoke", "nightly"], "Test": true, "Suite": false,'
    b' "Edit": true, "Versions": false, "secure-write": true, "Prune": false}'
)


def _spy_requests(properties: bytes = _PROPS):
    """Record (method, url, form) of each request; answer properties with `properties`."""
    calls: list[tuple[str, str, dict]] = []

    def spy(req, timeout=None):
        from urllib.parse import parse_qs
        form = parse_qs(req.data.decode()) if req.data else {}
        calls.append((req.get_method(), req.full_url, {k: v[0] for k, v in form.items()}))
        if "properties" in req.full_url and "format=json" in req.full_url:
            return _FakeResponse(properties)
        return _FakeResponse(b"saved", "text/html")

    return calls, patch("urllib.request.urlopen", side_effect=spy)


@pytest.mark.asyncio
async def test_save_page_content_keeps_help_text_and_tags():
    calls, p = _spy_requests()
    with p:
        async with Client(my_server.mcp) as c:
            await c.call_tool("fitnesse_save_page_content", {"page_path": "SuiteA.TestB", "page_content": "x"})
    method, url, form = calls[-1]
    assert method == "POST" and "saveData" in url
    assert form == {
        "pageContent": "x",
        "editTime": str(2**63 - 1),
        "helpText": "Old help",
        "suites": "smoke,nightly",
    }


@pytest.mark.asyncio
async def test_save_page_content_explicit_values_skip_lookup_and_empty_clears():
    calls, p = _spy_requests()
    with p:
        async with Client(my_server.mcp) as c:
            await c.call_tool(
                "fitnesse_save_page_content",
                {"page_path": "A", "page_content": "x", "help_text": "", "suites": "new"},
            )
    assert len(calls) == 1
    assert calls[0][2] == {"pageContent": "x", "editTime": str(2**63 - 1), "suites": "new"}


_MERGE_FORM = (
    b'<form method="post"><input type="hidden" name="responder" value="saveData"/>'
    b'<fieldset id="merge-new"></fieldset><fieldset id="merge-old">'
    b'<textarea id="oldContent" name="oldContent" readonly="readonly">theirs</textarea>'
    b"</fieldset></form>"
)


@pytest.mark.asyncio
async def test_save_page_content_with_edit_time_reports_conflict():
    calls: list[dict] = []

    def spy(req, timeout=None):
        from urllib.parse import parse_qs
        calls.append({k: v[0] for k, v in parse_qs(req.data.decode()).items()})
        return _FakeResponse(_MERGE_FORM, "text/html")

    with patch("urllib.request.urlopen", side_effect=spy):
        async with Client(my_server.mcp) as c:
            with pytest.raises(ToolError, match="Not saved: A was changed by someone else"):
                await c.call_tool(
                    "fitnesse_save_page_content",
                    {
                        "page_path": "A",
                        "page_content": "mine",
                        "edit_time": "1730000000000",
                        "ticket_id": "-42",
                        "help_text": "",
                        "suites": "",
                    },
                )
    assert calls == [{"pageContent": "mine", "editTime": "1730000000000", "ticketId": "-42"}]


@pytest.mark.asyncio
async def test_save_page_content_rejects_non_numeric_edit_time():
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool(
                "fitnesse_save_page_content",
                {"page_path": "A", "page_content": "x", "edit_time": "yesterday"},
            )


_EDIT_FORM = (
    b'<form method="post"><input type="hidden" name="responder" value="saveData"/>\n'
    b' <input type="hidden" name="editTime" value="1793983817067"/>\n'
    b' <input type="hidden" name="ticketId" value="-5646912860185278610"/>\n</form>'
)


def _page_data_server(edit_form: bytes | None = _EDIT_FORM):
    calls: list[str] = []

    def spy(req, timeout=None):
        calls.append(req.full_url)
        if "responder=edit" in req.full_url:
            if edit_form is None:
                raise HTTPError(req.full_url, 403, "Forbidden", {}, None)
            return _FakeResponse(edit_form, "text/html")
        return _FakeResponse(b"!1 Title\n|table|", "text/plain")

    return calls, patch("urllib.request.urlopen", side_effect=spy)


@pytest.mark.asyncio
async def test_get_page_data_returns_edit_session_for_conflict_check():
    calls, p = _page_data_server()
    with p:
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_data", {"page_path": "A"})
    assert result.data["body"] == "!1 Title\n|table|"
    assert result.data["edit_time"] == "1793983817067"
    assert result.data["ticket_id"] == "-5646912860185278610"
    # editTime is taken before the content is read.
    assert ["responder=edit" in u for u in calls] == [True, False]


@pytest.mark.asyncio
async def test_get_page_data_without_edit_form_still_returns_text():
    _, p = _page_data_server(edit_form=None)
    with p:
        async with Client(my_server.mcp) as c:
            result = await c.call_tool("fitnesse_get_page_data", {"page_path": "A"})
    assert result.data["ok"] is True and result.data["body"] == "!1 Title\n|table|"
    assert result.data["edit_time"] is None and result.data["ticket_id"] is None


@pytest.mark.asyncio
async def test_save_page_content_aborts_when_properties_unreadable():
    calls, p = _spy_requests(properties=b"<html>login</html>")
    with p:
        async with Client(my_server.mcp) as c:
            with pytest.raises(ToolError, match="nothing was saved"):
                await c.call_tool("fitnesse_save_page_content", {"page_path": "A", "page_content": "x"})
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.asyncio
async def test_save_properties_merges_onto_current():
    calls, p = _spy_requests()
    with p:
        async with Client(my_server.mcp) as c:
            await c.call_tool("fitnesse_save_properties", {"page_path": "A", "help_text": "New"})
    assert calls[-1][2] == {
        "PageType": "Test",
        "Suites": "smoke,nightly",
        "HelpText": "New",
        "Edit": "on",
        "secure-write": "on",
    }


@pytest.mark.asyncio
async def test_save_properties_explicit_attributes_replace():
    calls, p = _spy_requests()
    with p:
        async with Client(my_server.mcp) as c:
            await c.call_tool(
                "fitnesse_save_properties",
                {
                    "page_path": "A",
                    "page_type": "Static",
                    "suites": "",
                    "help_text": "",
                    "attributes": ["DisableTestHistory", "secure-read"],
                },
            )
    assert len(calls) == 1
    assert calls[0][2] == {"PageType": "Static", "DisableTestHistory": "on", "secure-read": "on"}


@pytest.mark.parametrize("bad", [{"page_type": "Normal"}, {"attributes": ["Secure-Edit"]}, {"attributes": ["Test"]}])
@pytest.mark.asyncio
async def test_save_properties_rejects_outdated_names(bad):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("fitnesse_save_properties", {"page_path": "A", **bad})


# ---------------------------------------------------------------------------
# Parameters and responders of the RestfulServices documentation (20261101)
# ---------------------------------------------------------------------------

@pytest.fixture
def complete_env(clean_env):
    os.environ["FITNESSE_TOOLSETS"] = "all,complete"
    importlib.reload(my_server)
    yield


async def _query(tool: str, args: dict) -> list[str]:
    """Call tool against a stub and return the query items of the single GET it sent."""
    calls, p = _spy_requests()
    with p:
        async with Client(my_server.mcp) as c:
            await c.call_tool(tool, args)
    assert len(calls) == 1
    return calls[0][1].split("?", 1)[1].split("&")


@pytest.mark.asyncio
async def test_search_scope_and_method_search():
    q = await _query(
        "fitnesse_search",
        {"search_string": "|click|x|", "method_search": True, "search_scope": "root"},
    )
    assert {"responder=search", "searchType=content", "isMethodSearch", "searchScope=root"} <= set(q)


@pytest.mark.asyncio
async def test_method_search_needs_content():
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="content"):
            await c.call_tool("fitnesse_search", {"search_string": "x", "search_type": "title", "method_search": True})


@pytest.mark.asyncio
async def test_run_suite_selection_partition_and_format():
    q = await _query(
        "fitnesse_run_suite",
        {
            "page_path": "S",
            "run_tests_matching_any_tag": "a,b",
            "run_tests_matching_all_tags": "c",
            "partition_count": 3,
            "partition_index": 2,
            "format": "junit",
        },
    )
    assert {"format=junit", "runTestsMatchingAnyTag=a%2Cb", "runTestsMatchingAllTags=c",
            "partitionCount=3", "partitionIndex=2"} <= set(q)


@pytest.mark.parametrize("args", [{"partition_count": 2}, {"partition_count": 2, "partition_index": 2}])
@pytest.mark.asyncio
async def test_run_suite_rejects_bad_partition(args):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="partition"):
            await c.call_tool("fitnesse_run_suite", {"page_path": "S", **args})


@pytest.mark.asyncio
async def test_run_test_nohistory_and_text_format():
    q = await _query("fitnesse_run_test", {"page_path": "T", "nohistory": True, "format": "text"})
    assert {"nohistory", "format=text"} <= set(q)


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("fitnesse_move_page", {"page_path": "A.B", "new_location": "C", "refactor_references": True}, {"refactorReferences"}),
        ("fitnesse_rename_page", {"page_path": "A.B", "new_name": "D", "refactor_references": True}, {"refactorReferences"}),
        ("fitnesse_purge_history", {"days": 7, "purge_global": True}, {"purgeGlobal=true"}),
        ("fitnesse_delete_page", {"page_path": "A", "delete_versions": True}, {"deleteVersions=yes"}),
        ("fitnesse_add_child_page", {"parent_page_path": "A", "page_name": "B", "help_text": "h", "suites": "t"}, {"helpText=h", "suites=t"}),
        ("fitnesse_view_version", {"page_path": "A", "version": "v1", "variables": {"X": "1"}}, {"version=v1", "X=1"}),
        ("fitnesse_get_suite_overview", {"page_path": "S", "suite_filter": "x"}, {"responder=overview", "suiteFilter=x"}),
        ("fitnesse_compare_versions", {"page_path": "A", "versions": ["v1", "v2"]}, {"Version_v1", "Version_v2"}),
        ("fitnesse_replace", {"page_path": "A", "search_string": "a+", "replacement_string": "b", "method_replace": True},
         {"responder=replace", "searchString=a%2B", "replacementString=b", "isMethodReplace"}),
    ],
)
@pytest.mark.asyncio
async def test_documented_inputs_forwarded(tool, args, expected):
    assert expected <= set(await _query(tool, args))


@pytest.mark.asyncio
async def test_execution_log_rejects_bad_date(complete_env):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("fitnesse_get_execution_log", {"page_path": "A", "result_date": "yesterday"})


@pytest.mark.asyncio
async def test_search_properties_uses_compatible_names(complete_env):
    q = await _query(
        "fitnesse_search_properties",
        {
            "page_path": "A",
            "page_types": ["Test", "Suite"],
            "actions": ["Edit"],
            "security": ["Any", "secure-read"],
            "special": ["SetUp"],
            "prune": True,
            "search_scope": "root",
        },
    )
    assert {"responder=searchProperties", "PageType=Test%2CSuite", "Action=Edit", "Security=Any",
            "Special=SetUp", "Prune", "searchScope=root"} <= set(q)


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("fitnesse_partition_preview", {"page_path": "S", "partition_count": 4}, {"responder=partition", "partitionCount=4", "format=tsv"}),
        ("fitnesse_get_execution_log", {"page_path": "A"}, {"responder=executionLog", "resultDate=latest"}),
        ("fitnesse_get_proxy_data", {"page_path": "A", "type": "meat", "version": "v1"}, {"type=meat", "version=v1"}),
        ("fitnesse_get_refactor_screen", {"page_path": "A", "refactor_type": "move"}, {"type=move"}),
        ("fitnesse_get_test_history", {"format": "html", "results": 5, "trim": "Suite."}, {"results=5", "trim=Suite."}),
    ],
)
@pytest.mark.asyncio
async def test_complete_toolset_documented_inputs(complete_env, tool, args, expected):
    assert expected <= set(await _query(tool, args))


@pytest.mark.parametrize("line,masked", [
    ("Authorization=Bearer mcp-token-123", "Authorization=Bearer ***"),
    ("[INFO ] 2026-11-09 18:54:34.345 [main] FITLIBRARYWEB - Authorization=Bearer t", "[INFO ] 2026-11-09 18:54:34.345 [main] FITLIBRARYWEB - Authorization=Bearer ***"),
    ("X-Api-Key=k-456", "X-Api-Key=***"),
    ('    "Authorization": "Basic dXNlcjpwdw==",', '    "Authorization": "Basic ***",'),
    ('"client_secret": "a b c", "x": 1', '"client_secret": "***", "x": 1'),
    ("&quot;password&quot;: &quot;two words&quot;, &quot;user&quot;: &quot;u&quot;", "&quot;password&quot;: &quot;***&quot;, &quot;user&quot;: &quot;u&quot;"),
    ("mcpcookie:value", "mcpcookie:***"),
    ("Cookie=a=1; b=2", "Cookie=***"),
    ("set-cookie=JSESSIONID=abc; Path=/; HttpOnly", "set-cookie=***"),
    ("GET https://x/api?access_token=abc&y=1", "GET https://x/api?access_token=***&y=1"),
    ("Command: java -Ddb.password=xyz -cp a.jar", "Command: java -Ddb.password=*** -cp a.jar"),
    # no secrets: unchanged
    ("Accept=*/*", "Accept=*/*"),
    ("content-type=application/json", "content-type=application/json"),
    ("FITLIBRARYWEB - Response Headers:", "FITLIBRARYWEB - Response Headers:"),
    ("Authorization failed for user", "Authorization failed for user"),
    ("token count 5", "token count 5"),
])
def test_mask_secrets_in_logs(line, masked):
    assert my_server._mask_secrets(line) == masked


@pytest.mark.asyncio
async def test_execution_log_masks_secrets(complete_env):
    with _patch_urlopen(body=b"<pre>Authorization=Bearer abc\nX-Api-Key=k</pre>", content_type="text/html"):
        async with Client(my_server.mcp) as c:
            data = (await c.call_tool("fitnesse_get_execution_log", {"page_path": "A"})).data
    assert data["body"] == "<pre>Authorization=Bearer ***\nX-Api-Key=***</pre>"


@pytest.mark.asyncio
async def test_test_history_results_need_html(complete_env):
    async with Client(my_server.mcp) as c:
        with pytest.raises(ToolError, match="html"):
            await c.call_tool("fitnesse_get_test_history", {"results": 5})


@pytest.mark.asyncio
async def test_save_account_gated_and_posts_form(clean_env):
    async with Client(my_server.mcp) as c:
        assert "fitnesse_save_account" not in {t.name for t in await c.list_tools()}
    os.environ["FITNESSE_ALLOW_ACCOUNT_ADMIN"] = "1"
    try:
        importlib.reload(my_server)
        calls, p = _spy_requests()
        with p:
            async with Client(my_server.mcp) as c:
                await c.call_tool("fitnesse_save_account", {"action": "delete_user", "user_name": "bob"})
                with pytest.raises(ToolError):
                    await c.call_tool("fitnesse_save_account", {"action": "create_user", "user_name": "x"})
        method, url, form = calls[0]
        assert method == "POST" and "responder=saveAccount" in url
        assert form == {"deleteUser": "yes", "UserNameText": "bob"}
    finally:
        os.environ.pop("FITNESSE_ALLOW_ACCOUNT_ADMIN", None)


def test_fresh_import_with_every_gate_open():
    """importlib.reload keeps old globals, so only a fresh process catches definition-order bugs."""
    import subprocess
    import sys
    gates = {
        "FITNESSE_TOOLSETS": "all,debug,complete", "FITNESSE_FILES_ROOT": "files", "FITNESSE_ALLOW_SHUTDOWN": "1",
        "FITNESSE_ALLOW_ACCOUNT_ADMIN": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, **gates},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# FITNESSE_AUTH_MODE
# ---------------------------------------------------------------------------

def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.fixture
def auth_env(monkeypatch):
    """Reload the server with the given FITNESSE_AUTH_MODE and a server account in the env."""
    monkeypatch.setenv("FITNESSE_USERNAME", "server-account")
    monkeypatch.setenv("FITNESSE_PASSWORD", "server-secret")

    def load(mode: str | None):
        if mode is None:
            monkeypatch.delenv("FITNESSE_AUTH_MODE", raising=False)
        else:
            monkeypatch.setenv("FITNESSE_AUTH_MODE", mode)
        importlib.reload(my_server)

    yield load
    monkeypatch.delenv("FITNESSE_AUTH_MODE", raising=False)
    monkeypatch.undo()
    importlib.reload(my_server)


async def _call_over_http(headers: dict[str, str], urls: list[str] | None = None) -> list[str | None]:
    """Call one tool through the real HTTP stack; return the Authorization headers sent to FitNesse
    (and collect the URLs called into urls)."""
    import asyncio
    import socket

    import uvicorn
    from fastmcp.client.transports import StreamableHttpTransport

    sent: list[str | None] = []

    def spy(req, timeout=None):
        sent.append(req.get_header("Authorization"))
        if urls is not None:
            urls.append(req.full_url)
        return _FakeResponse()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(my_server.mcp.http_app(), port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        with patch("urllib.request.urlopen", side_effect=spy):
            async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers)) as c:
                await c.call_tool("fitnesse_get_properties", {"page_path": "FrontPage"})
    finally:
        server.should_exit = True
        await task
    return sent


@pytest.mark.asyncio
async def test_auth_env_mode_uses_server_account(auth_env):
    auth_env(None)
    sent = await _call_over_http({my_server.HEADER_USERNAME: "bob", my_server.HEADER_PASSWORD: "pw"})
    assert sent == [_basic("server-account", "server-secret")]


@pytest.mark.asyncio
async def test_auth_headers_mode_uses_each_callers_login(auth_env):
    auth_env("headers")
    for user, password in (("alice", "a-pw"), ("bob", "b-pw")):
        sent = await _call_over_http({my_server.HEADER_USERNAME: user, my_server.HEADER_PASSWORD: password})
        assert sent == [_basic(user, password)]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-FitNesse-Username": "bob"}])
async def test_auth_headers_mode_never_falls_back_to_server_account(auth_env, headers):
    auth_env("headers")
    with pytest.raises(ToolError, match="X-FitNesse-Username and X-FitNesse-Password"):
        await _call_over_http(headers)
    with _patch_urlopen() as urlopen, pytest.raises(ToolError, match="FITNESSE_AUTH_MODE=headers"):
        async with Client(my_server.mcp) as c:  # in-memory: no HTTP request, no headers
            await c.call_tool("fitnesse_get_properties", {"page_path": "FrontPage"})
    urlopen.assert_not_called()


def test_auth_headers_mode_keeps_debug_sessions_apart(auth_env):
    auth_env("headers")
    with patch.object(my_server, "get_http_headers", return_value={"x-fitnesse-username": "alice", "x-fitnesse-password": "a"}):
        alice = my_server._debug_state()
    with patch.object(my_server, "get_http_headers", return_value={"x-fitnesse-username": "bob", "x-fitnesse-password": "b"}):
        bob = my_server._debug_state()
    assert alice is not bob


@pytest.mark.asyncio
async def test_url_header_picks_the_instance(auth_env, monkeypatch):
    auth_env(None)
    monkeypatch.setenv("FITNESSE_BASE_URL", "http://default:8080")
    urls: list[str] = []
    await _call_over_http({}, urls)
    await _call_over_http({my_server.HEADER_BASE_URL: "https://other.example.com:8443/"}, urls)
    assert urls[0].startswith("http://default:8080/FrontPage?")
    assert urls[1].startswith("https://other.example.com:8443/FrontPage?")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x", "other.example.com", "http://x/?a=1", "http://"])
def test_url_header_must_be_http(url):
    with patch.object(my_server, "get_http_headers", return_value={"x-fitnesse-url": url}):
        with pytest.raises(ToolError, match="must be an http"):
            my_server._base_url()


def test_url_header_keeps_debug_sessions_apart(auth_env):
    auth_env(None)
    with patch.object(my_server, "get_http_headers", return_value={"x-fitnesse-url": "http://a:8080"}):
        a = my_server._debug_state()
    with patch.object(my_server, "get_http_headers", return_value={"x-fitnesse-url": "http://b:8080/"}):
        b = my_server._debug_state()
    with patch.object(my_server, "get_http_headers", return_value={}):
        default = my_server._debug_state()
    assert len({id(a), id(b), id(default)}) == 3


def test_auth_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("FITNESSE_AUTH_MODE", "basic")
    with pytest.raises(RuntimeError, match="FITNESSE_AUTH_MODE='basic'"):
        my_server._auth_mode_from_env()


def test_auth_headers_mode_refuses_stdio(auth_env, monkeypatch):
    import fastmcp

    auth_env("headers")
    monkeypatch.setattr(fastmcp.settings, "transport", "stdio")
    with patch.object(my_server.mcp, "run") as run, pytest.raises(SystemExit, match="HTTP transport"):
        my_server.main()
    run.assert_not_called()
