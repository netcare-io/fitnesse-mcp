# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the KDTA debug tools (responder=debugTest / debugTable)."""
import importlib
import io
import json
import os
from email.message import Message
from unittest.mock import patch
from urllib import parse
from urllib.error import HTTPError

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server as my_server

GATES = ("FITNESSE_TOOLSETS",)


def _reload(**env):
    for key in GATES:
        os.environ.pop(key, None)
    os.environ.update({"FITNESSE_TOOLSETS": "read,write,test,debug", **env})
    importlib.reload(my_server)


@pytest.fixture
def debug_env():
    previous = {k: os.environ.get(k) for k in GATES}
    _reload(FITNESSE_TOOLSETS="all,debug")
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)


class _Resp:
    def __init__(self, status, body, content_type="application/json", cookies=()):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        for cookie in cookies:
            self.headers["Set-Cookie"] = cookie

    def read(self, n):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FakeFitNesse:
    """Answers urlopen calls from a list of (status, body) and records the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, req, timeout=None):
        form = dict(parse.parse_qsl(req.data.decode(), keep_blank_values=True))
        self.requests.append({"url": req.full_url, "form": form, "headers": dict(req.header_items()), "timeout": timeout})
        status, body, *rest = self.answers.pop(0)
        resp = _Resp(status, body, *rest)
        if status >= 400:
            raise HTTPError(req.full_url, status, "err", resp.headers, io.BytesIO(resp._body))
        return resp

    @property
    def actions(self):
        return [r["form"]["action"] for r in self.requests]


async def _call(tool, args=None, *answers):
    fake = FakeFitNesse(*answers)
    with patch("urllib.request.urlopen", side_effect=fake):
        async with Client(my_server.mcp) as c:
            result = await c.call_tool(tool, args or {})
    return result.data, fake


async def _start(page="FrontPage.T", suite=False):
    await _call(
        "fitnesse_debug_start", {"page_path": page, "suite": suite},
        (200, {"running": False}),
        (200, {"started": True, "page": page, "session": "s1"}),
        *([(200, {"page": page, "pages": []})] if suite else []),
    )


RESULT_HTML = (
    '<table><tr><td>check</td><td class="pass">a &amp; b</td></tr>'
    '<tr><td class="fail">x<br>y</td><td><table><tr><td>n1</td><td>n2</td></tr></table></td></tr></table>'
    "<p>between</p><table><tr><td class=\"error\">" + "E" * 3000 + "</td></tr></table>"
)


def test_parse_tables_spans_and_cells():
    tables = my_server._parse_tables(RESULT_HTML)
    assert len(tables) == 2
    assert RESULT_HTML[tables[0]["start"]:tables[0]["end"]].startswith("<table><tr><td>check")
    assert RESULT_HTML[tables[0]["start"]:tables[0]["end"]].endswith("</table></td></tr></table>")
    row0, row1 = tables[0]["rows"]
    assert row0[1] == {"text": "a & b", "status": "pass"}
    assert row1[0]["status"] == "fail"
    assert "n1" in row1[1]["text"] and "n2" in row1[1]["text"]


def test_tables_text_marks_status_and_cuts_long_cells():
    text = my_server._tables_text(RESULT_HTML)
    assert "| check | [pass] a & b |" in text
    assert "[fail] x ⏎ y" in text
    assert "chars cut]" in text and "Table 1:" in text


@pytest.mark.asyncio
async def test_debug_tools_hidden_by_default():
    previous = {k: os.environ.get(k) for k in GATES}
    try:
        _reload(FITNESSE_TOOLSETS="all")
        async with Client(my_server.mcp) as c:
            names = {t.name for t in await c.list_tools()}
        assert not any("debug" in n or n.startswith("fitnesse_table_") for n in names)
        assert not hasattr(my_server, "fitnesse_debug_start")  # not even registered
        _reload(FITNESSE_TOOLSETS="all,debug")
        async with Client(my_server.mcp) as c:
            names = {t.name for t in await c.list_tools()}
        assert "fitnesse_debug_run_wiki" in names and "fitnesse_table_source" in names
        assert "fitnesse_table_save" in names
        _reload(FITNESSE_TOOLSETS="read,write,test")
        async with Client(my_server.mcp) as c:
            names = {t.name for t in await c.list_tools()}
        assert not any(n.startswith(("fitnesse_debug_", "fitnesse_table_")) for n in names)
        debug = {"read": {"fitnesse_debug_status", "fitnesse_debug_page_tables", "fitnesse_debug_inspect",
                          "fitnesse_table_source"},
                 "write": {"fitnesse_table_save", "fitnesse_table_insert", "fitnesse_table_delete"},
                 "test": {"fitnesse_debug_start", "fitnesse_debug_run_wiki", "fitnesse_debug_run_page",
                          "fitnesse_debug_end"}}
        for toolsets, modes in (("read,debug", {"read"}), ("write,debug", {"write"}),
                                ("test,debug", {"test"}), ("read,test,debug", {"read", "test"})):
            _reload(FITNESSE_TOOLSETS=toolsets)
            async with Client(my_server.mcp) as c:
                names = {t.name for t in await c.list_tools()}
            assert {n for n in names if n.startswith(("fitnesse_debug_", "fitnesse_table_"))} \
                == set().union(*(debug[m] for m in modes)), toolsets
        with pytest.raises(RuntimeError, match="need at least one of"):
            _reload(FITNESSE_TOOLSETS="debug")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)


@pytest.mark.asyncio
async def test_start_refuses_running_session(debug_env):
    with pytest.raises(ToolError, match="already running"):
        await _call(
            "fitnesse_debug_start", {"page_path": "FrontPage.T"},
            (200, {"running": True, "session": "browser", "page": "X", "currentPage": "X"}),
        )


@pytest.mark.asyncio
async def test_start_replace_and_session_forwarded(debug_env):
    data, fake = await _call(
        "fitnesse_debug_start", {"page_path": "FrontPage.T", "replace": True},
        (200, {"running": True, "session": "browser"}),
        (200, {"started": True, "page": "FrontPage.T", "session": "s1"}),
    )
    assert data["session"] == "s1"
    assert fake.actions == ["status", "start"]
    assert "session" not in fake.requests[0]["form"]
    assert fake.requests[1]["headers"]["X-requested-with"] == "XMLHttpRequest"
    assert "responder=debugTest" in fake.requests[1]["url"]

    data, fake = await _call(
        "fitnesse_debug_run_wiki", {"wiki": "|with browser|"},
        (200, {"html": RESULT_HTML, "counts": {"right": 1, "wrong": 1, "ignores": 0, "exceptions": 1}}),
    )
    assert fake.requests[0]["form"] == {"action": "runWiki", "session": "s1", "wiki": "|with browser|"}
    assert fake.requests[0]["timeout"] == my_server.DEBUG_TIMEOUT_SECONDS
    assert "[pass] a & b" in data["tables"]
    assert data["page_totals"]["wrong"] == 1 and "html" not in data


@pytest.mark.asyncio
async def test_replaced_session_is_forgotten(debug_env):
    await _start()
    with pytest.raises(ToolError, match="replaced"):
        await _call("fitnesse_debug_inspect", {"kind": "scope"}, (409, {"error": "Session replaced", "code": "replaced"}))
    with pytest.raises(ToolError, match="no debug session"):
        await _call("fitnesse_debug_inspect", {"kind": "scope"})


@pytest.mark.asyncio
async def test_non_json_answer_reported_as_login_problem(debug_env):
    with pytest.raises(ToolError, match="login"):
        await _call("fitnesse_debug_status", {}, (200, b"<html>SSO</html>", "text/html"))


@pytest.mark.asyncio
async def test_state_queries_parsed(debug_env):
    await _start()
    variables = (
        "<table><tr><td>dynamicVariables</td><td>name</td><td>value</td><td>system</td></tr>"
        "<tr><td></td><td>user</td><td>a&lt;b</td><td>false</td></tr>"
        "<tr><td></td><td>java.version</td><td>21</td><td>true</td></tr></table>"
    )
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "dynamicVariables"}, (200, {"html": variables, "counts": {}}))
    assert data["dynamicVariables"] == [{"name": "user", "value": "a<b", "system": False}]

    live = '<table><tr><td>liveView</td><td>http://hub/wd/hub</td><td>abc</td><td>[{"name":"chrome","hub":"h","session":"abc"}]</td></tr></table>'
    data, _ = await _call(
        "fitnesse_debug_inspect", {"kind": "liveView"},
        (200, {"html": live, "counts": {}, "liveViewUrl": "http://hub/ui", "liveViewPassword": "secret", "liveViewOnly": False}),
    )
    assert data["browsers"][0]["name"] == "chrome" and data["selenium_session"] == "abc"
    assert "secret" not in json.dumps(data)


@pytest.mark.asyncio
async def test_keywords_filter(debug_env):
    await _start()
    kw = [{"name": "Browser", "children": [
        {"syntax": "with browser", "helpText": "opens"},
        {"syntax": "click %1", "parameters": [{"label": "locator"}]},
    ]}]
    html = "<table><tr><td>keywordsJson</td><td>" + json.dumps(kw).replace("&", "&amp;").replace("<", "&lt;") + "</td><td>[]</td></tr></table>"
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "keywords"}, (200, {"html": html, "counts": {}}))
    assert data == {"count": 2, "categories": {"Browser": ["with browser", "click %1"]}}
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "keywords", "filter": "LOCATOR"}, (200, {"html": html, "counts": {}}))
    assert data["matches"] == 1 and data["keywords"][0]["syntax"] == "click %1"


@pytest.mark.asyncio
async def test_run_page_changes_page_and_selects_tables(debug_env):
    await _start("FrontPage.Suite", suite=True)
    page_html = "<div><table><tr><td>setup</td></tr></table><table><tr><td>t1</td></tr></table><table><tr><td>t2</td></tr></table></div>"
    data, fake = await _call(
        "fitnesse_debug_run_page", {"page_path": "FrontPage.Suite.A", "tables": [2, 0]},
        (200, {"html": "", "counts": {}, "page": "FrontPage.Suite.A"}),
        (200, {"page": "FrontPage.Suite.A", "html": page_html}),
        (200, {"html": "<table><tr><td class=\"pass\">t2</td></tr></table>", "counts": {"right": 1}}),
    )
    assert fake.actions == ["nextPage", "pageHtml", "runTable"]
    assert fake.requests[2]["form"]["html"] == "<table><tr><td>t2</td></tr></table>\n<table><tr><td>setup</td></tr></table>"
    assert data["previous_page_teardown"]["teardown_of"] == "FrontPage.Suite"
    assert data["page"] == "FrontPage.Suite.A"

    # same page again: no nextPage
    _, fake = await _call(
        "fitnesse_debug_run_page", {},
        (200, {"page": "FrontPage.Suite.A", "html": page_html}),
        (200, {"html": "", "counts": {}}),
    )
    assert fake.actions == ["pageHtml", "runTable"]


@pytest.mark.asyncio
async def test_status_reads_log_and_end_forgets_session(debug_env):
    await _start()
    lines = [{"stream": "out", "text": f"l{i}", "time": "t"} for i in range(5)]
    data, fake = await _call(
        "fitnesse_debug_status", {"max_log_lines": 3},
        (200, {"running": True, "session": "s1", "page": "FrontPage.T"}),
        (200, {"lines": lines, "next": 5, "dropped": 0}),
    )
    assert fake.actions == ["status", "log"]
    assert data["known_to_this_server"] is True
    assert data["log"]["more"] is True and data["log"]["lines"][0] == "t [out] l0"
    data, fake = await _call("fitnesse_debug_end", {}, (200, {"ended": True, "html": "", "counts": {}, "log": {"lines": lines[3:]}}))
    assert fake.requests[0]["form"]["logFrom"] == "3"
    assert data["ended"] is True and len(data["log"]) == 2
    with pytest.raises(ToolError, match="no debug session"):
        await _call("fitnesse_debug_page_tables", {})


@pytest.mark.asyncio
async def test_status_adopts_running_session(debug_env):
    data, fake = await _call(
        "fitnesse_debug_status", {"adopt": True, "max_log_lines": 0},
        (200, {"running": True, "session": "browser", "page": "P", "currentPage": "P.A", "suite": True}),
    )
    assert data["known_to_this_server"] is True and fake.actions == ["status"]
    _, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|x|"}, (200, {"html": "", "counts": {}}))
    assert fake.requests[0]["form"]["session"] == "browser"
    assert "P?responder" in fake.requests[0]["url"]


@pytest.mark.asyncio
async def test_run_wiki_restart_ends_storytest_first(debug_env):
    await _start()
    data, fake = await _call(
        "fitnesse_debug_run_wiki", {"wiki": "|x|", "restart": True},
        (200, {"html": "", "counts": {}, "page": "FrontPage.T"}),
        (200, {"html": "", "counts": {}}),
    )
    assert fake.actions == ["nextPage", "runWiki"]
    assert "page" not in fake.requests[0]["form"]
    assert data["previous_page_teardown"]["teardown_of"] == "FrontPage.T"


@pytest.mark.asyncio
async def test_start_suite_lists_pages(debug_env):
    data, fake = await _call(
        "fitnesse_debug_start", {"page_path": "S", "suite": True, "suite_filter": "smoke"},
        (200, {"running": False}),
        (200, {"started": True, "page": "S", "session": "s1"}),
        (200, {"page": "S", "pages": [{"path": "S.A", "kind": "test", "runnable": True}]}),
    )
    assert fake.actions == ["status", "start", "suitePages"]
    assert fake.requests[1]["form"]["suite"] == "true"
    assert fake.requests[2]["form"]["suiteFilter"] == "smoke"
    assert data["pages"][0]["path"] == "S.A"


@pytest.mark.asyncio
async def test_table_save_block_and_conflict(debug_env):
    _, fake = await _call(
        "fitnesse_table_save", {"page_path": "P", "index": 1, "last": 2, "original": "o", "wiki": "w"},
        (200, {"index": 1, "wiki": "w", "html": ""}),
    )
    assert fake.requests[0]["form"] == {"action": "saveBlock", "index": "1", "last": "2", "original": "o", "wiki": "w"}
    assert "responder=debugTable" in fake.requests[0]["url"]
    with pytest.raises(ToolError, match="page changed"):
        await _call(
            "fitnesse_table_delete", {"page_path": "P", "index": 0, "original": "o"},
            (409, {"error": "Page was modified"}),
        )


@pytest.mark.asyncio
async def test_debug_instructions_sent_only_with_visible_debug_tools():
    import re
    previous = {k: os.environ.get(k) for k in GATES}
    try:
        for env, expected in (({"FITNESSE_TOOLSETS": "all"}, False), ({"FITNESSE_TOOLSETS": "all,debug"}, True),
                              ({"FITNESSE_TOOLSETS": "read,write,test"}, False),
                              ({"FITNESSE_TOOLSETS": "read,debug"}, False),
                              ({"FITNESSE_TOOLSETS": "test,debug"}, False),
                              ({"FITNESSE_TOOLSETS": "read,test,debug"}, True),
                              ({"FITNESSE_TOOLSETS": "all,debug,complete"}, True)):
            _reload(**env)
            async with Client(my_server.mcp) as c:
                instructions = c.instructions or ""
                names = {t.name for t in await c.list_tools()}
            async with Client(my_server.mcp, mode="legacy") as c:
                assert (c.initialize_result.instructions or "") == instructions
            assert ("fitnesse_debug_start" in instructions) is expected, env
            if expected:
                # every full tool name the agent is told about must exist
                assert set(re.findall(r"fitnesse_\w+", instructions)) - {"fitnesse_table_"} <= names
                toolsets = env["FITNESSE_TOOLSETS"]
                assert "fitnesse_table_source" in instructions
                assert ("Editing tables" in instructions) is ("write" in toolsets or "all" in toolsets), env
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)
