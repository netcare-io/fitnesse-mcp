# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the KDTA debug tools (responder=debugTest / debugTable)."""
import importlib
import io
import json
import logging
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
    for name in [n for n in vars(my_server) if n.startswith("fitnesse_debug_")]:
        delattr(my_server, name)  # reload keeps old attributes; registration is what's tested
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
    def __init__(self, status, body, content_type="application/json", cookies=(), chunk=None, cut=None):
        self.status = status
        self._chunk = chunk  # at most this many bytes per read, as http.client may return
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if cut is not None:  # the connection ends after cut bytes of the announced body
            self.headers["Content-Length"] = str(len(self._body))
            self._body = self._body[:cut]
        for cookie in cookies:
            self.headers["Set-Cookie"] = cookie

    def read(self, n):
        n = min(n, self._chunk or n)
        data, self._body = self._body[:n], self._body[n:]
        return data

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
        if isinstance(self.answers[0], BaseException):
            raise self.answers.pop(0)
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
        (200, {"page": page, "pages": []}),  # suitePages: of the suite, or the page's SuiteSetUp/-TearDown
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


PNG = b"\x89PNG\r\n\x1a\nfake"
PNG_SRC = "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()
MEDIA_HTML = (
    '<p>see <a href="/FrontPage.Help">help</a></p>'
    '<table><tr><td>take screenshot</td><td class="pass">ok <img src="' + PNG_SRC + '" alt="shot"></td></tr>'
    '<tr><td><a href="http://ci/report">report</a></td><td><a href="javascript:void(0)">x</a>'
    '<img src="/files/images/collapsableOpen.gif"><img src="/files/shots/s.png"></td></tr></table>'
    '<div class="collapsible closed"><p class="title">show after</p>'
    '<div>log line 1<br>line 2<div>nested</div></div></div>'
    '<div class="other">not a folding area</div>'
)


_IF = "<tr><td{c}>if</td><td>get</td><td>x</td><td>is</td><td>true</td></tr><tr><td{c}>then</td><td>set</td><td>y</td></tr>"
CONDITION_TRUE_HTML = (
    "<table><tr><td>condition</td></tr>" + _IF.format(c="")
    + "<tr><td><b>Result of then</b></td><td colspan=\"4\"><table border=\"1\">"
    "<tr><td>set</td><td>y</td></tr>"
    "<tr><td>get</td><td>y</td><td>is</td><td class=\"fail\">false <span>expected</span><hr>true <span>actual</span></td></tr>"
    "</table></td></tr></table>"
)


def test_tables_text_says_what_a_condition_did():
    text = my_server._tables_text(CONDITION_TRUE_HTML)
    assert "Table 0 – condition: then ran" in text
    assert "[fail] false expected" in text  # a nested cell keeps its status
    assert my_server._failed(my_server._parse_tables(CONDITION_TRUE_HTML)[0])
    assert "Table 0:" in my_server._tables_text(CONDITION_TRUE_HTML, only_failures=True)

    false_html = "<table><tr><td>condition</td></tr>" + _IF.format(c="") + "</table>"
    assert "condition: condition false, then did not run" in my_server._tables_text(false_html)
    assert not my_server._failed(my_server._parse_tables(false_html)[0])

    else_html = false_html.replace("</table>", "<tr><td>Result of else</td><td><table><tr><td>x</td></tr></table></td></tr></table>")
    assert "condition: else ran" in my_server._tables_text(else_html)

    error_html = (
        "<table><tr><td colspan=\"5\" class=\"error\">condition<hr/> <span class=\"fit_label\"><b>if</b> statement "
        "must return <u>true</u> or <u>false</u>.</span></td></tr>" + _IF.format(c=' class="ignore"') + "</table>"
    )
    assert "condition: condition error, then and else did not run" in my_server._tables_text(error_html)

    skipped_html = "<table><tr><td class=\"ignore\">condition</td></tr>" + _IF.format(c=' class="ignore"').replace(
        "<td>", "<td class=\"ignore\">") + "</table>"
    assert "condition: skipped, not run" in my_server._tables_text(skipped_html)

    assert "condition:" not in my_server._tables_text(FAILING_HTML)


def test_tables_text_lists_images_links_and_folding_areas():
    text = my_server._tables_text(MEDIA_HTML)
    assert text.startswith("Before the first table – link: help -> /FrontPage.Help")
    assert "| take screenshot | [pass] ok [image 1] |" in text
    assert "Table 0 – link in row 2, column 1: report -> http://ci/report" in text
    assert "javascript" not in text and "collapsable" not in text
    assert "Table 0 – image 2 in row 2, column 2: /files/shots/s.png" in text
    assert "Table 0 – after: show after:\n  log line 1\n  line 2\n  nested" in text
    assert "not a folding area" not in text
    assert "data:image" not in text


async def _call_content(tool, args, *answers):
    fake = FakeFitNesse(*answers)
    with patch("urllib.request.urlopen", side_effect=fake):
        async with Client(my_server.mcp) as c:
            return await c.call_tool(tool, args)


@pytest.mark.asyncio
async def test_run_wiki_attaches_inline_images(debug_env):
    await _start()
    result = await _call_content(
        "fitnesse_debug_run_wiki", {"wiki": "|take screenshot|"}, (200, {"html": MEDIA_HTML, "counts": {}}),
    )
    assert [c.type for c in result.content] == ["text", "image"]
    assert __import__("base64").b64decode(result.content[1].data) == PNG
    assert result.content[1].mime_type == "image/png"
    assert result.structured_content["images"] == [{"image": 1, "format": "png", "content": 1}]
    assert json.loads(result.content[0].text) == result.structured_content
    assert "_images" not in result.structured_content


@pytest.mark.asyncio
async def test_run_wiki_caps_images_and_keeps_plain_result_without_them(debug_env):
    await _start()
    html = "<table><tr>" + ('<td><img src="' + PNG_SRC + '"></td>') * 5 + '<td><img src="data:image/png;base64,!!"></td></tr></table>'
    result = await _call_content("fitnesse_debug_run_wiki", {"wiki": "|x|"}, (200, {"html": html, "counts": {}}))
    assert [c.type for c in result.content] == ["text"] + ["image"] * my_server.DEBUG_MAX_IMAGES
    images = result.structured_content["images"]
    assert [i.get("content") for i in images[:4]] == [1, 2, 3, 4]
    assert "omitted" in images[4] and images[5]["omitted"] == "invalid base64"

    result = await _call_content("fitnesse_debug_run_wiki", {"wiki": "|x|"}, (200, {"html": RESULT_HTML, "counts": {}}))
    assert [c.type for c in result.content] == ["text"] and "images" not in result.structured_content


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
                          "fitnesse_debug_defined_action_source", "fitnesse_table_source"},
                 "write": {"fitnesse_table_save", "fitnesse_table_insert", "fitnesse_table_delete",
                           "fitnesse_table_move", "fitnesse_table_save_text", "fitnesse_table_move_text"},
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
        (200, {"pages": []}),
    )
    assert data["session"] == "s1"
    assert fake.actions == ["status", "start", "suitePages"]
    assert "session" not in fake.requests[0]["form"]
    assert fake.requests[1]["headers"]["X-requested-with"] == "XMLHttpRequest"
    assert "responder=debugTest" in fake.requests[1]["url"]

    data, fake = await _call(
        "fitnesse_debug_run_wiki", {"wiki": "|with browser|"},
        (200, {"html": RESULT_HTML, "counts": {"right": 1, "wrong": 1, "ignores": 0, "exceptions": 1}}),
        (200, {"html": "<table><tr><td>own</td></tr></table>"}),  # pageHtml: no SetUp, so no hint
    )
    assert fake.requests[0]["form"] == {"action": "runWiki", "session": "s1", "wiki": "|with browser|"}
    assert fake.requests[0]["timeout"] == my_server.DEBUG_TIMEOUT_SECONDS
    assert "[pass] a & b" in data["tables"]
    assert data["page_totals"]["wrong"] == 1 and "html" not in data


def _entry(session, page, **extra):
    return {"session": session, "page": page, "suite": False, "busy": False, "startedAt": 1, **extra}


@pytest.mark.asyncio
async def test_start_with_several_sessions_per_account(debug_env):
    # mod #159: one session per page and account; others' sessions elsewhere don't matter
    browser = _entry("b1", "Other.Page")
    data, fake = await _call(
        "fitnesse_debug_start", {"page_path": ".FrontPage.T"},
        (200, {"running": True, "sessions": [browser], **browser}),  # the only one: status describes it
        (200, {"started": True, "page": "FrontPage.T", "session": "s1"}),
        (200, {"pages": []}),
    )
    assert fake.actions == ["status", "start", "suitePages"] and data["session"] == "s1"

    # this server's own session: keep using it here, end it before going elsewhere
    with pytest.raises(ToolError, match="already running on FrontPage.T: keep using it"):
        await _call("fitnesse_debug_start", {"page_path": "FrontPage.T"},
                    (200, {"running": True, "sessions": [browser, _entry("s1", "FrontPage.T")]}))
    with pytest.raises(ToolError, match="still running on FrontPage.T"):
        await _call("fitnesse_debug_start", {"page_path": "Third"},
                    (200, {"running": False, "sessions": [browser, _entry("s1", "FrontPage.T")]}))
    await _call("fitnesse_debug_end", {}, (200, {"lines": [], "next": 0}), (200, {"ended": True, "html": "", "counts": {}}))

    # someone's session on the same page: refused, with how to adopt it
    with pytest.raises(ToolError, match=r"already running on Other.Page.*adopt=true, session='b1'"):
        await _call("fitnesse_debug_start", {"page_path": "Other.Page"},
                    (200, {"running": True, "sessions": [browser], **browser}))
    _, fake = await _call(
        "fitnesse_debug_start", {"page_path": "Other.Page", "replace": True},
        (200, {"running": True, "sessions": [browser], **browser}),
        (200, {"started": True, "page": "Other.Page", "session": "s2"}),
        (200, {"pages": []}),
    )
    assert fake.actions == ["status", "start", "suitePages"]


@pytest.mark.asyncio
async def test_status_lists_and_adopts_one_of_several_sessions(debug_env):
    sessions = [_entry("b1", "A"), _entry("b2", "B", suite=True)]
    data, _ = await _call("fitnesse_debug_status", {"max_log_lines": 0}, (200, {"running": False, "sessions": sessions}))
    assert data["known_to_this_server"] is False and [s["session"] for s in data["sessions"]] == ["b1", "b2"]
    with pytest.raises(ToolError, match="Several debug sessions.*b1 on A, b2 on B"):
        await _call("fitnesse_debug_status", {"adopt": True}, (200, {"running": False, "sessions": sessions}))
    with pytest.raises(ToolError, match="No running debug session x"):
        await _call("fitnesse_debug_status", {"adopt": True, "session": "x"}, (200, {"running": False, "sessions": sessions}))
    data, _ = await _call("fitnesse_debug_status", {"adopt": True, "session": "b2", "max_log_lines": 0},
                          (200, {"running": False, "sessions": sessions}))
    assert data["known_to_this_server"] is True
    _, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|x|"}, (200, {"html": "", "counts": {}}))
    assert fake.requests[0]["form"]["session"] == "b2" and "/B?responder" in fake.requests[0]["url"]
    # known by id even when status describes another page's session
    data, _ = await _call("fitnesse_debug_status", {"page_path": "A", "max_log_lines": 0},
                          (200, {"running": True, "sessions": sessions, **sessions[0]}))
    assert data["known_to_this_server"] is True


@pytest.mark.asyncio
async def test_session_codes_of_mod_159_have_hints(debug_env):
    await _start()
    with pytest.raises(ToolError, match="sessionRequired.*fitnesse_debug_status lists them"):
        await _call("fitnesse_debug_inspect", {"kind": "scope"},
                    (409, {"code": "sessionRequired", "error": "Several debug sessions are running"}))
    with pytest.raises(ToolError, match="tooManySessions.*maxSessionsPerUser"):
        await _call("fitnesse_debug_start", {"page_path": "P", "replace": True},
                    (200, {"running": False, "sessions": []}),
                    (429, {"code": "tooManySessions", "error": "Too many debug sessions"}))


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
async def test_debug_answer_read_to_the_end_when_reads_come_short(debug_env):
    # http.client's read(n) may stop early; a large keywordsJson answer was cut off that way
    data, _ = await _call("fitnesse_debug_status", {}, (200, {"running": False, "pad": "x" * 5000}, "application/json", (), 7))
    assert data["running"] is False


@pytest.mark.asyncio
async def test_debug_answer_that_ends_early_is_fetched_again_only_for_reading_actions(debug_env):
    full = {"running": False, "pad": "x" * 500}
    data, fake = await _call("fitnesse_debug_status", {}, (200, full, "application/json", (), None, 100), (200, full))
    assert data["running"] is False and fake.actions == ["status", "status"]
    with pytest.raises(ToolError, match="after 100 of .* bytes .* Tried 3 times"):
        await _call("fitnesse_debug_status", {}, *[(200, full, "application/json", (), None, 100)] * 3)

    await _start()
    with pytest.raises(ToolError, match="(?s)after 10 of .*'runWiki'.*may have taken effect.*fitnesse_debug_status") as err:
        await _call("fitnesse_debug_run_wiki", {"wiki": "|x|"}, (200, {"html": "", "counts": {}}, "application/json", (), None, 10))
    assert "Tried" not in str(err.value)  # runWiki changes the session: not repeated


@pytest.mark.asyncio
async def test_busy_process_is_asked_again_later_for_reading_actions(debug_env):
    await _start()
    busy = (423, {"error": "busy", "code": "busy"})
    with patch("time.sleep") as sleep:
        data, fake = await _call("fitnesse_debug_inspect", {"kind": "scope"}, busy, busy,
                                 (200, {"html": "<table><tr><td>scope</td></tr></table>"}))
    assert fake.actions == ["scope"] * 3 and [c.args[0] for c in sleep.call_args_list] == [2.0, 5.0]
    with patch("time.sleep"), pytest.raises(ToolError, match="HTTP 423.*Tried 3 times"):
        await _call("fitnesse_debug_inspect", {"kind": "scope"}, busy, busy, busy)
    with patch("time.sleep") as sleep, pytest.raises(ToolError, match="HTTP 423") as err:
        await _call("fitnesse_debug_run_wiki", {"wiki": "|x|"}, busy)
    assert not sleep.called and "Tried" not in str(err.value)  # a run is never repeated


@pytest.mark.asyncio
async def test_end_uses_log_max_where_the_fork_has_it(debug_env):
    await _start()
    lines = [{"stream": "out", "text": f"l{i}", "time": "t"} for i in range(30)]
    data, fake = await _call("fitnesse_debug_end", {}, (200, {"lines": [], "next": 45}),
                             (200, {"ended": True, "html": "", "counts": {}, "log": {"lines": lines, "skipped": 2}}))
    assert fake.requests[1]["form"]["logMax"] == "30" and fake.requests[1]["form"]["logFrom"] == "15"
    assert data["log_omitted"] == 17 and len(data["log"]) == 30  # 15 before logFrom, 2 left out by FitNesse
    await _start()
    _, fake = await _call("fitnesse_debug_end", {"max_log_lines": 0}, (200, {"lines": [], "next": 45}),
                          (200, {"ended": True, "html": "", "counts": {}}))
    assert "logMax" not in fake.requests[1]["form"]


@pytest.mark.asyncio
async def test_debug_connection_closed_without_answer(debug_env):
    import http.client
    data, fake = await _call("fitnesse_debug_status", {}, http.client.BadStatusLine(""), (200, {"running": False}))
    assert data["running"] is False and fake.actions == ["status", "status"]
    await _start()
    with pytest.raises(ToolError, match=r"closed the connection without a complete answer \(RemoteDisconnected: gone\) to 'runWiki'.*may have taken effect"):
        await _call("fitnesse_debug_run_wiki", {"wiki": "|x|"}, http.client.RemoteDisconnected("gone"))


@pytest.mark.asyncio
async def test_json_answer_that_is_no_object_shows_what_arrived(debug_env, caplog):
    body = b'{"ended": true, "log": ["' + b"x" * 500 + b'", "cut'
    with caplog.at_level(logging.WARNING), pytest.raises(ToolError) as err:
        await _call("fitnesse_debug_status", {}, (200, body))
    message = str(err.value)
    assert "invalid JSON" in message and f"{len(body)} bytes received" in message
    assert "'{\"ended\": true" in message and "\", \"cut'" in message  # start and end of the body
    assert "login" not in message
    assert body.decode() in caplog.text
    with pytest.raises(ToolError, match="parsed as list, not an object"):
        await _call("fitnesse_debug_status", {}, (200, b"[]"))


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


VARIABLES_HTML = (
    "<table><tr><td>dynamicVariables</td><td>name</td><td>value</td><td>system</td></tr>"
    "<tr><td></td><td>user</td><td>bob</td><td>false</td></tr></table>"
)
LIVE_VIEW = {
    "html": '<table><tr><td>liveView</td><td>http://hub/wd/hub</td><td>abc</td><td>[]</td></tr></table>',
    "counts": {}, "liveViewUrl": "http://hub/ui", "liveViewPassword": "secret", "liveViewOnly": False,
}
PASS_HTML = "<table><tr><td class=\"pass\">ok</td></tr></table>"


@pytest.mark.asyncio
async def test_run_extras_come_with_the_run(debug_env):
    await _start()
    data, fake = await _call(
        "fitnesse_debug_run_wiki", {"wiki": "|ok|", "with_variables": True, "with_live_view": True},
        (200, {"html": PASS_HTML, "counts": {"right": 1}, "dynamicVariables": VARIABLES_HTML, "liveView": LIVE_VIEW}),
    )
    assert fake.actions == ["runWiki"]
    assert fake.requests[0]["form"]["withDynamicVariables"] == "true" and fake.requests[0]["form"]["withLiveView"] == "true"
    assert data["dynamicVariables"] == [{"name": "user", "value": "bob", "system": False}]
    assert data["liveView"]["selenium_session"] == "abc" and data["liveView"]["live_view_url"] == "http://hub/ui"
    assert "secret" not in json.dumps(data)

    data, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|ok|"}, (200, {"html": PASS_HTML, "counts": {"right": 1}}))
    assert "withDynamicVariables" not in fake.requests[0]["form"] and "withLiveView" not in fake.requests[0]["form"]
    assert "dynamicVariables" not in data and "liveView" not in data


@pytest.mark.asyncio
async def test_run_extras_fall_back_to_the_actions_on_an_older_fork(debug_env):
    await _start()
    data, fake = await _call(
        "fitnesse_debug_run_page", {"with_variables": True, "with_live_view": True},
        (200, {"page": "FrontPage.T", "html": PASS_HTML}),
        (200, {"html": PASS_HTML, "counts": {"right": 1}}),  # the fork ignored the extras
        (200, {"html": VARIABLES_HTML, "counts": {}}),
        (200, LIVE_VIEW),
    )
    assert fake.actions[-3:] == ["runTable", "dynamicVariables", "liveView"]
    assert data["dynamicVariables"][0]["name"] == "user"
    assert data["liveView"]["hub"] == "http://hub/wd/hub" and "secret" not in json.dumps(data)


FAILING_HTML = (
    "<table><tr><td class=\"pass\">ok</td></tr></table>"
    "<table><tr><td>check</td><td class=\"fail\">1 expected 2</td></tr></table>"
    "<table><tr><td class=\"pass\">ok too</td></tr></table>"
    "<table><tr><td class=\"error\">boom</td></tr></table>"
)


@pytest.mark.asyncio
async def test_run_page_only_failures(debug_env):
    await _start()
    data, _ = await _call(
        "fitnesse_debug_run_page", {"only_failures": True},
        (200, {"page": "FrontPage.T", "html": FAILING_HTML}),
        (200, {"html": FAILING_HTML, "counts": {"right": 2, "wrong": 1, "exceptions": 1}}),
    )
    assert "Table 1:" in data["tables"] and "Table 3:" in data["tables"]  # usual numbers
    assert "Table 0:" not in data["tables"] and "Table 2:" not in data["tables"]
    assert data["tables_without_failures"] == 2 and data["counts"]["wrong"] == 1
    data, _ = await _call(
        "fitnesse_debug_run_page", {},
        (200, {"page": "FrontPage.T", "html": FAILING_HTML}),
        (200, {"html": FAILING_HTML, "counts": {}}),
    )
    assert "Table 0:" in data["tables"] and "tables_without_failures" not in data


@pytest.mark.asyncio
async def test_keywords_filter(debug_env):
    await _start()
    kw = [{"name": "Browser", "children": [
        {"syntax": "with browser", "helpText": "opens"},
        {"syntax": "click %1", "parameters": [{"label": "locator"}]},
    ]}]
    html = "<table><tr><td>keywordsJson</td><td>" + json.dumps(kw).replace("&", "&amp;").replace("<", "&lt;") + "</td><td>[]</td></tr></table>"
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "keywords"}, (200, {"html": html, "counts": {}}))
    assert data["count"] == 2 and data["categories"] == {"Browser": 2} and "filter" in data["hint"]
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "keywords", "filter": "LOCATOR"}, (200, {"html": html, "counts": {}}))
    assert data["matches"] == 1 and data["keywords"][0]["syntax"] == "click %1"


@pytest.mark.asyncio
async def test_keywords_filter_leaves_out_help_texts(debug_env):
    await _start()
    kw = [{"name": "Web", "children": [
        {"syntax": "click %1", "helpText": "clicks", "tableItems": [
            {"syntax": "with %1", "helpText": "This statement is mandatory."},
            {"syntax": "or %1", "helpText": "optional"},
        ]},
        {"syntax": "click hard %1", "helpText": "clicks harder"},
    ]}]
    html = "<table><tr><td>keywordsJson</td><td>" + json.dumps(kw).replace("&", "&amp;").replace("<", "&lt;") + "</td><td>[]</td></tr></table>"
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "keywords", "filter": "clicks", "limit": 1},
                          (200, {"html": html, "counts": {}}))
    assert data["matches"] == 2 and len(data["keywords"]) == 1  # the filter still searches help texts
    entry = data["keywords"][0]
    assert "helpText" not in entry
    assert entry["tableItems"] == [{"syntax": "with %1", "mandatory": True}, {"syntax": "or %1"}]
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "keywords", "filter": "clicks", "details": True},
                          (200, {"html": html, "counts": {}}))
    assert data["keywords"][0]["helpText"] == "clicks"
    assert data["keywords"][0]["tableItems"][0]["helpText"] == "This statement is mandatory."


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
    long = {"stream": "info", "text": "Command: java -cp " + "x.jar:" * 1000, "time": "t"}
    assert my_server._log_line(long).endswith(f"… ({len(long['text'])} chars)")
    assert len(my_server._log_line(long)) < 600
    data, fake = await _call("fitnesse_debug_end", {}, (200, {"lines": [], "next": 5, "dropped": 0}),
                             (200, {"ended": True, "html": "", "counts": {}, "log": {"lines": lines[3:]}}))
    assert fake.actions == ["log", "end"] and fake.requests[0]["form"]["from"] == "999999999"
    assert fake.requests[1]["form"]["logFrom"] == "3"  # the unread lines, fewer than 30
    assert data["ended"] is True and len(data["log"]) == 2 and "log_omitted" not in data
    with pytest.raises(ToolError, match="no debug session"):
        await _call("fitnesse_debug_page_tables", {})


@pytest.mark.asyncio
async def test_end_returns_only_the_last_log_lines(debug_env):
    await _start()
    lines = [{"stream": "out", "text": f"l{i}", "time": "t"} for i in range(40)]
    # FitNesse is asked only for the last 30 lines: logFrom = total - 30
    data, fake = await _call("fitnesse_debug_end", {}, (200, {"lines": [], "next": 40}),
                             (200, {"ended": True, "html": "", "counts": {}, "log": {"lines": lines[10:]}}))
    assert fake.requests[1]["form"]["logFrom"] == "10"
    assert data["log_omitted"] == 10 and len(data["log"]) == 30 and data["log"][0] == "t [out] l10"
    # without logFrom FitNesse sends no log at all
    await _start()
    data, fake = await _call("fitnesse_debug_end", {"max_log_lines": 0}, (200, {"lines": [], "next": 40}),
                             (200, {"ended": True, "html": "", "counts": {}}))
    assert "logFrom" not in fake.requests[1]["form"]
    assert "log" not in data and data["log_omitted"] == 40
    # an older fork or a failing log call: every unread line, cut here
    await _start()
    data, fake = await _call("fitnesse_debug_end", {}, (500, {"error": "boom"}),
                             (200, {"ended": True, "html": "", "counts": {}, "log": {"lines": lines}}))
    assert fake.requests[1]["form"]["logFrom"] == "0"
    assert data["log_omitted"] == 10 and len(data["log"]) == 30


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


# As debugTable action=source answers, and since mod #157 every edit as 'source'.
SOURCE = {"tables": ["|note|hello2|"], "texts": ["Scratch\n\n", "\n"], "page": "DebugScratch"}


@pytest.mark.asyncio
async def test_table_edits_return_the_page_after_the_change(debug_env):
    args = {"page_path": "DebugScratch", "index": 0, "original": "|note|hello|", "wiki": "|note|hello2|"}
    data, fake = await _call("fitnesse_table_save", args, (200, {"index": 0, "wiki": "|note|hello2|", "source": SOURCE}))
    assert fake.actions == ["save"]  # mod #157: no second request
    assert data["source"] == SOURCE

    data, fake = await _call("fitnesse_table_save", args, (200, {"index": 0, "wiki": "|note|hello2|"}), (200, SOURCE))
    assert fake.actions == ["save", "source"]  # older mod: read here
    assert data["source"] == SOURCE and "hint" not in data

    data, fake = await _call("fitnesse_table_delete", {"page_path": "P", "index": 0, "original": "o"},
                             (200, {"index": 0}), (500, {"error": "boom"}))
    assert fake.actions == ["delete", "source"]
    assert "source" not in data and "Saved, but reading the page again failed" in data["hint"]


@pytest.mark.asyncio
async def test_table_save_block_and_conflict(debug_env):
    _, fake = await _call(
        "fitnesse_table_save", {"page_path": "P", "index": 1, "last": 2, "original": "o", "wiki": "w"},
        (200, {"index": 1, "wiki": "w", "html": "", "source": SOURCE}),
    )
    assert fake.actions == ["saveBlock"]
    assert fake.requests[0]["form"] == {"action": "saveBlock", "index": "1", "last": "2", "original": "o", "wiki": "w"}
    assert "responder=debugTable" in fake.requests[0]["url"]
    with pytest.raises(ToolError, match="page changed"):
        await _call(
            "fitnesse_table_delete", {"page_path": "P", "index": 0, "original": "o"},
            (409, {"error": "Page was modified"}),
        )


def _defined_row(wiki, note, author, page, overrides=""):
    wiki = wiki.replace("<", "&lt;").replace(">", "&gt;")
    return f"<tr><td>defined</td><td>{wiki}</td><td>{note}</td><td>{author}</td><td>{page}</td><td>{overrides}</td></tr>"


# As the keywords action answers (shape taken from a live session): header, scope keywords, defined actions.
KEYWORDS_HTML = (
    "<table><tr><td>keywords</td><td>wiki</td><td>tooltip</td></tr>"
    "<tr><td>simple</td><td>|''&lt;i&gt;to&lt;/i&gt;''|string|</td><td>Returns the string</td></tr>"
    + _defined_row("|''<i>click by js</i>''|locator|", "Clicks a web element by js", "Dominik", "Training.DS.Modules")
    + _defined_row("|''<i>start with url</i>''|url|''<i>and login with</i>''|credentials|",
                   "Deprecated. Opens login page", "Vojislav", ".Lib.Login", ".Old.Login")
    + "</table>"
)
EMPTY_KEYWORDS_HTML = KEYWORDS_HTML.split("<tr><td>defined")[0] + "</table>"


def test_defined_actions_parsed_from_keywords_answer():
    click, login = my_server._defined_actions(KEYWORDS_HTML)
    assert click == {
        "action": "|click by js|locator|", "keywords": ["click by js"], "parameters": ["locator"],
        "note": "Clicks a web element by js", "deprecated": False, "author": "Dominik",
        "page": "Training.DS.Modules", "overrides": [],
    }
    assert login["action"] == "|start with url|url|and login with|credentials|"
    assert login["keywords"] == ["start with url", "and login with"]
    assert login["deprecated"] and login["page"] == "Lib.Login" and login["overrides"] == ["Old.Login"]


@pytest.mark.asyncio
async def test_inspect_defined_actions(debug_env):
    await _start()
    data, fake = await _call("fitnesse_debug_inspect", {"kind": "definedActions", "filter": "LOGIN"},
                             OLD_MOD, (200, {"html": KEYWORDS_HTML, "counts": {}}))
    assert fake.actions == ["definedActions", "keywords"]  # older mod: parsed from the keywords HTML
    assert data["count"] == 2 and data["matches"] == 1
    assert data["definedActions"][0]["page"] == "Lib.Login"
    data, _ = await _call("fitnesse_debug_inspect", {"kind": "definedActions"},
                          OLD_MOD, (200, {"html": EMPTY_KEYWORDS_HTML, "counts": {}}))
    assert data["definedActions"] == [] and "define actions at" in data["hint"]


UNKNOWN_ACTION = (400, {"error": "Unknown action 'definedAction'; expected source, delete, insert, move, moveText, save, saveBlock, saveText."})
# a mod before #156: debugTest has no definedActions and answers 400 without a code
OLD_MOD = (400, {"error": "Unknown action 'definedActions'"})


@pytest.mark.asyncio
async def test_inspect_defined_actions_from_the_json(debug_env):
    await _start()
    entry = {"action": "|greet|name|", "page": ".Lib.Js", "overrides": [".Old.Js"], "note": "says hi",
             "author": "ds", "deprecated": True}
    data, fake = await _call("fitnesse_debug_inspect", {"kind": "definedActions"}, (200, {"definedActions": [entry]}))
    assert fake.actions == ["definedActions"] and fake.requests[0]["form"]["session"] == "s1"
    assert data["definedActions"] == [{
        "action": "|greet|name|", "keywords": ["greet"], "parameters": ["name"], "note": "says hi",
        "deprecated": True, "author": "ds", "page": "Lib.Js", "overrides": ["Old.Js"],
    }]
    # unknownAction (mod #156 and later) also falls back to the keywords HTML
    data, fake = await _call("fitnesse_debug_inspect", {"kind": "definedActions"},
                             (400, {"error": "Unknown action", "code": "unknownAction"}),
                             (200, {"html": KEYWORDS_HTML, "counts": {}}))
    assert fake.actions == ["definedActions", "keywords"] and data["count"] == 2


@pytest.mark.asyncio
async def test_changed_definition_says_how_to_reload_it(debug_env):
    await _start()
    with pytest.raises(ToolError, match=r"Reload: fitnesse_debug_run_wiki\('\|define actions at\|\.Lib\.Js\|'\), then fitnesse_debug_defined_action_source again"):
        await _call("fitnesse_debug_defined_action_source", {"action": "|greet|bob|"},
                    (409, {"error": "Lib.Js changed; run |define actions at|.Lib.Js|", "code": "changed", "page": "Lib.Js"}))
    # a save conflict has no code: read the source again
    with pytest.raises(ToolError, match="Read fitnesse_table_source again"):
        await _call("fitnesse_table_save", {"page_path": "P", "index": 0, "original": "o", "wiki": "w"},
                    (409, {"error": "The page has been changed in the meantime; reload it to edit the current version"}))


# Two defined actions separated by ----; the second's body repeats the first's header (doesn't count).
DEFINITIONS_SOURCE = {
    "tables": ["|click by js|locator|\n", "|run script|...|\n", "|start with url|url|and login with|credentials|\n",
               "|click by js|//login|\n", "|check|logged in|\n"],
    "texts": ["", "\n", "\n----\n", "\n!3 body\n", "\n", "\n"],
}


def test_definition_block_follows_fitlibrary_rules():
    click, login = my_server._defined_actions(KEYWORDS_HTML)
    assert my_server._definition_block(DEFINITIONS_SOURCE, click) == {
        "index": 0, "last": 1, "original": "|click by js|locator|\n\n|run script|...|\n",
    }
    block = my_server._definition_block(DEFINITIONS_SOURCE, login)
    assert (block["index"], block["last"]) == (2, 4)
    assert block["original"].startswith("|start with url|") and "!3 body" in block["original"]

    single = {"tables": ["|click by js|locator|\n"], "texts": ["", ""]}
    assert my_server._definition_block(single, click)["last"] is None
    twice = {"tables": ["|click by js|locator|\n", "|click by js|x|\n"], "texts": ["", "\n----\n", ""]}
    with pytest.raises(ToolError, match="2 times"):
        my_server._definition_block(twice, click)
    with pytest.raises(ToolError, match="No definition"):
        my_server._definition_block({"tables": ["|other|\n"], "texts": ["", ""]}, click)


def test_separators_are_exactly_four_dashes_outside_literals_and_comments():
    # as FitNesse renders them (fork: exactly four dashes become <hr/>, anywhere between two tables)
    for text, splits in (("\n----\n", True), ("\n---- next\n", True), ("\n-----\n", False),
                         ("\n------\n", False), ("  ----\n", True), ("text ---- text", True),
                         ("x----y", True), ("\n----", True),  # "----|b|": the table follows on the line
                         ("!-\n----\n-!", False), ("{{{\n----\n}}}", False), ("--!-x-!--", False),
                         ("#----\n", False), ("#c\n----\n", True), ("text\n", False)):
        assert my_server._separates(text) is splits, text


@pytest.mark.asyncio
async def test_defined_action_source_reads_defining_page_fresh(debug_env):
    await _start()
    data, fake = await _call(
        "fitnesse_debug_defined_action_source", {"action": "start with url and login with"},
        OLD_MOD,
        (200, {"html": KEYWORDS_HTML, "counts": {}}),
        UNKNOWN_ACTION,
        (200, DEFINITIONS_SOURCE),
    )
    assert fake.actions == ["definedActions", "keywords", "definedAction", "source"]  # bare keywords -> call row first
    assert fake.requests[2]["form"]["name"] == "|start with url|url|and login with|credentials|"
    assert "/Lib.Login?responder=debugTable" in fake.requests[3]["url"]
    assert (data["page"], data["index"], data["last"]) == ("Lib.Login", 2, 4)
    with pytest.raises(ToolError, match="not among"):
        await _call("fitnesse_debug_defined_action_source", {"action": "|nope|"},
                    UNKNOWN_ACTION, OLD_MOD, (200, {"html": KEYWORDS_HTML, "counts": {}}))
    with pytest.raises(ToolError, match="No defined actions loaded"):
        await _call("fitnesse_debug_defined_action_source", {"action": "x"},
                    OLD_MOD, (200, {"html": EMPTY_KEYWORDS_HTML, "counts": {}}))


@pytest.mark.asyncio
async def test_defined_action_source_from_the_fork(debug_env):
    block = {"action": "|''<i>greet</i>''|name|", "page": ".Lib.Js", "index": 3, "last": 4, "original": "o"}
    with pytest.raises(ToolError, match="no debug session"):
        await _call("fitnesse_debug_defined_action_source", {"action": "|greet|bob|"})
    await _start()
    data, fake = await _call("fitnesse_debug_defined_action_source", {"action": "|greet|bob|"}, (200, block))
    assert fake.requests[0]["form"] == {"action": "definedAction", "name": "|greet|bob|", "session": "s1"}
    assert "/FrontPage.T?responder=debugTable" in fake.requests[0]["url"]
    assert data == {"action": "|greet|name|", "page": "Lib.Js", "index": 3, "last": 4, "original": "o"}
    with pytest.raises(ToolError, match="notLoaded.*number of\narguments|notLoaded"):
        await _call("fitnesse_debug_defined_action_source", {"action": "|greet|}"},
                    (404, {"error": "No defined action for |greet|", "code": "notLoaded"}))
    # unknownPlace (e.g. define actions at ... from another root): looked up as on older forks
    data, fake = await _call(
        "fitnesse_debug_defined_action_source", {"action": "|other|x|"},
        (404, {"error": "Not on a page of defined actions", "code": "unknownPlace"}),
        (200, {"definedActions": [{"action": "|''other''|x|", "page": ".Other.Defs"}]}),
        (200, {"tables": ["|''other''|x|", "|log message|@{x}|", "|''next''|"], "texts": ["", "\n\n", "\n---- next\n", "\n"]}),
    )
    assert fake.actions == ["definedAction", "definedActions", "source"]
    assert data == {"action": "|other|x|", "page": "Other.Defs", "index": 0, "last": 1,
                    "original": "|''other''|x|\n\n|log message|@{x}|"}
    # unknownPlace for a definition the page no longer has (lib #83): the session keeps it until it ends
    with pytest.raises(ToolError, match="(?s)Other.Defs no longer has \\|other\\|x\\|.*until it ends"):
        await _call(
            "fitnesse_debug_defined_action_source", {"action": "|other|x|"},
            (404, {"error": "Not on the page any more", "code": "unknownPlace"}),
            (200, {"definedActions": [{"action": "|''other''|x|", "page": ".Other.Defs"}]}),
            (200, {"tables": ["|''greet''|", "|log message|hi|", "|''other''|x|"], "texts": ["", "\n\n", "\n-----\n", "\n"]}),
        )
    with pytest.raises(ToolError, match="(?s)unknownPlace.*define action\\| table.*failed as well"):
        await _call("fitnesse_debug_defined_action_source", {"action": "|inline|"},
                    (404, {"error": "Defined in a table", "code": "unknownPlace"}),
                    (200, {"definedActions": []}))
    # a test process with an older FitLibrary: the fork says unsupported, the MCP cuts the block itself
    data, fake = await _call(
        "fitnesse_debug_defined_action_source", {"action": "|click by js|x|"},
        (501, {"error": "Test process too old", "code": "unsupported"}),
        OLD_MOD,
        (200, {"html": KEYWORDS_HTML, "counts": {}}),
        (200, {"tables": ["|click by js|locator|\n"], "texts": ["", ""]}),
    )
    assert fake.actions == ["definedAction", "definedActions", "keywords", "source"]
    assert (data["page"], data["index"], data["last"]) == ("Training.DS.Modules", 0, None)


@pytest.mark.asyncio
async def test_defined_action_source_by_page_without_session(debug_env):
    data, fake = await _call(
        "fitnesse_debug_defined_action_source",
        {"action": "|start with url|url|and login with|credentials|", "page_path": "Lib.Login"},
        (200, DEFINITIONS_SOURCE),
    )
    assert fake.actions == ["source"]  # the fork's definedAction needs a session
    assert (data["index"], data["last"]) == (2, 4)


@pytest.mark.asyncio
async def test_table_save_reloads_defined_actions(debug_env):
    args = {"page_path": "Lib.Login", "index": 2, "last": 4, "original": "o", "wiki": "w",
            "reload_defined_actions": True}
    data, fake = await _call("fitnesse_table_save", args, (200, {"index": 2, "wiki": "w"}), (200, SOURCE))
    assert fake.actions == ["saveBlock", "source"]  # no session: saved only
    assert data["reload"]["reloaded"] is False and "saved only" in data["reload"]["reason"]

    await _start()
    data, fake = await _call(
        "fitnesse_table_save", args,
        (200, {"index": 2, "wiki": "w"}),
        (200, SOURCE),
        (200, {"html": "<table><tr><td>define actions at</td></tr></table>", "counts": {"right": 0, "wrong": 0}}),
        OLD_MOD,
        (200, {"html": KEYWORDS_HTML, "counts": {}}),
    )
    assert fake.actions == ["saveBlock", "source", "runWiki", "definedActions", "keywords"]
    assert fake.requests[2]["form"]["wiki"] == "|define actions at|.Lib.Login|"
    assert data["reload"]["reloaded"] is True
    assert [a["action"] for a in data["reload"]["definedActions"]] == ["|start with url|url|and login with|credentials|"]

    data, _ = await _call(
        "fitnesse_table_save", args,
        (200, {"index": 2, "wiki": "w"}),
        (200, SOURCE),
        (500, {"error": "boom"}),
    )
    assert data["reload"]["reloaded"] is False and "Saved, but reloading failed" in data["reload"]["reason"]


@pytest.mark.asyncio
async def test_table_save_and_insert_let_the_fork_reload(debug_env):
    await _start()
    data, fake = await _call(
        "fitnesse_table_save",
        {"page_path": "Lib.Login", "index": 2, "last": 4, "original": "o", "wiki": "w", "reload_defined_actions": True},
        (200, {"index": 2, "wiki": "w", "reloaded": True, "reloadHtml": "<table></table>", "reloadCounts": {},
               "keywords": "<table>big</table>",
               "definedActions": [{"action": "|''<i>a</i>''|x|", "page": ".Lib.Login", "overrides": []},
                                  {"action": "|c|", "page": "Lib.Login", "overrides": [], "note": "old",
                                   "author": "ds", "deprecated": True},
                                  {"action": "|b|", "page": "Other", "overrides": []}],
               "source": SOURCE}),
    )
    assert fake.actions == ["saveBlock"]
    assert fake.requests[0]["form"]["reload"] == "true" and fake.requests[0]["form"]["session"] == "s1"
    assert data == {"index": 2, "wiki": "w", "source": SOURCE, "reload": {"reloaded": True, "definedActions": [
        {"action": "|a|x|"}, {"action": "|c|", "note": "old", "author": "ds", "deprecated": True}]}}
    data, fake = await _call(
        "fitnesse_table_insert",
        {"page_path": "Lib.Login", "index": 5, "before": "", "wiki": "|new action|\n", "reload_defined_actions": True},
        (200, {"added": 1, "html": "<table><tr><td>new action</td></tr></table>", "reloaded": False,
               "error": "busy", "code": "busy", "source": SOURCE}),
    )
    assert fake.actions == ["insert"]  # code/error in a 200 answer: saved, only the reload failed
    assert fake.requests[0]["form"]["start"] == "false" and fake.requests[0]["form"]["before"] == ""
    assert data["reload"] == {"reloaded": False, "error": "busy", "code": "busy"}
    assert "new action" in data["tables"] and "html" not in data and data["source"] == SOURCE


@pytest.mark.asyncio
async def test_reload_by_the_fork_sends_the_session_cookie(debug_env):
    # found live on 8091: without the cookie the fork answers reloaded=false, code=noSession
    await _call(
        "fitnesse_debug_start", {"page_path": "FrontPage.T"},
        (200, {"running": False}),
        (200, {"started": True, "page": "FrontPage.T", "session": "s1"}, "application/json",
         ["debugTestSession=abc; Path=/; HttpOnly"]),
        (200, {"pages": []}),
    )
    args = {"page_path": "Lib.Login", "index": 0, "original": "o", "wiki": "w"}
    _, fake = await _call("fitnesse_table_save", {**args, "reload_defined_actions": True},
                          (200, {"index": 0, "reloaded": True, "definedActions": [], "source": SOURCE}))
    assert fake.requests[0]["headers"].get("Cookie") == "debugTestSession=abc"
    _, fake = await _call("fitnesse_table_save", args, (200, {"index": 0, "source": SOURCE}))
    assert "Cookie" not in fake.requests[0]["headers"]  # a plain edit stays outside the session


@pytest.mark.asyncio
async def test_move_save_text_and_move_text_forms(debug_env):
    _, fake = await _call("fitnesse_table_move", {"page_path": "P", "index": 0, "original": "t", "to": 3, "start": True},
                          (200, {"ok": True, "source": SOURCE}))
    assert fake.requests[0]["form"] == {"action": "move", "index": "0", "original": "t", "to": "3", "start": "true"}
    data, fake = await _call("fitnesse_table_save_text", {"page_path": "P", "index": 1, "original": "a", "wiki": "b\n|x|\n"},
                             (200, {"added": 1, "html": "<table><tr><td>x</td></tr></table>", "source": SOURCE}))
    assert fake.requests[0]["form"] == {"action": "saveText", "index": "1", "original": "a", "wiki": "b\n|x|\n"}
    assert data["added"] == 1 and "| x |" in data["tables"]
    _, fake = await _call("fitnesse_table_move_text",
                          {"page_path": "P", "index": 1, "original": "a", "to": 3, "to_original": "c"},
                          (200, {}), (200, SOURCE))
    assert fake.requests[0]["form"] == {"action": "moveText", "index": "1", "original": "a", "to": "3",
                                        "toOriginal": "c", "start": "false"}


INCLUDE_PAGE_HTML = (
    '<div class="collapsible closed"><p class="title">Set Up: <a href="/Lib.SetUp">.Lib.SetUp</a>'
    ' <a href="/Lib.SetUp?edit&amp;redirectToReferer=true" class="edit">(edit)</a></p>'
    '<div><table><tr><td>start browser</td></tr></table>'
    '<div class="collapsible"><p class="title">Included page: <a class="edit" href="/Lib.Login?edit">edit</a></p>'
    '<div><table><tr><td>login</td></tr></table></div></div>'
    '<table><tr><td>after include</td></tr></table></div></div>'
    '<table><tr><td>own 0</td></tr></table><table><tr><td>own 1</td></tr></table>'
)


@pytest.mark.asyncio
async def test_page_tables_name_the_page_a_table_is_edited_on(debug_env):
    await _start()
    data, _ = await _call("fitnesse_debug_page_tables", {}, (200, {"html": INCLUDE_PAGE_HTML}))
    assert [(t["first_row"], t["source_page"], t["source_index"], t.get("included", False)) for t in data["tables"]] == [
        ("start browser", "Lib.SetUp", 0, True), ("login", "Lib.Login", 0, True),
        ("after include", "Lib.SetUp", 1, True), ("own 0", "FrontPage.T", 0, False), ("own 1", "FrontPage.T", 1, False),
    ]
    assert my_server._tables_text(INCLUDE_PAGE_HTML).count("after:") == 0  # includes are no folding areas
    assert data["setup_tables"] == [0, 1, 2]


TEARDOWN_HTML = (
    '<div class="collapsible"><p class="title"><a class="edit" href="/Lib.SetUp?edit">edit</a></p>'
    '<div><table><tr><td>setup</td></tr></table></div></div>'
    '<table><tr><td>own</td></tr></table>'
    '<div class="collapsible"><p class="title"><a class="edit" href="/Lib.TearDown?edit">edit</a></p>'
    '<div><table><tr><td>teardown</td></tr></table></div></div>'
)


def test_setup_indices_stop_at_the_page_and_skip_teardown():
    assert my_server._setup_indices(my_server._parse_tables(TEARDOWN_HTML)) == [0]
    assert my_server._setup_indices(my_server._parse_tables(INCLUDE_PAGE_HTML)) == [0, 1, 2]
    only_teardown = TEARDOWN_HTML.split("<table><tr><td>own</td></tr></table>")[1]
    assert my_server._setup_indices(my_server._parse_tables(only_teardown)) == []
    assert my_server._setup_indices(my_server._parse_tables("<table><tr><td>own</td></tr></table>")) == []


@pytest.mark.asyncio
async def test_run_page_setup_only_runs_the_setup_tables(debug_env):
    await _start()
    data, fake = await _call(
        "fitnesse_debug_run_page", {"setup_only": True},
        (200, {"page": "FrontPage.T", "html": TEARDOWN_HTML}),
        (200, {"html": "<table><tr><td class=\"pass\">setup</td></tr></table>", "counts": {"right": 1}}),
    )
    assert fake.actions == ["pageHtml", "runTable"]
    assert fake.requests[1]["form"]["html"] == "<table><tr><td>setup</td></tr></table>"
    assert "[pass] setup" in data["tables"]

    data, fake = await _call(
        "fitnesse_debug_run_page", {"setup_only": True},
        (200, {"page": "FrontPage.T", "html": "<table><tr><td>own</td></tr></table>"}),
    )
    assert fake.actions == ["pageHtml"]
    assert data["setup_tables"] == [] and "no SetUp" in data["hint"]


@pytest.mark.asyncio
async def test_run_wiki_failing_before_the_setup_hints_at_setup_only(debug_env):
    await _start()
    wrong = (200, {"html": "<table><tr><td class=\"error\">greet</td></tr></table>", "counts": {"exceptions": 1}})
    data, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|greet|"}, wrong, (200, {"html": TEARDOWN_HTML}))
    assert fake.actions == ["runWiki", "pageHtml"]  # the page has a SetUp
    assert "has not run in this test" in data["hint"] and "setup_only=true" in data["hint"]

    data, _ = await _call("fitnesse_debug_run_wiki", {"wiki": "|ok|"}, (200, {"html": "<table><tr><td class=\"pass\">ok</td></tr></table>", "counts": {"right": 1}}))
    assert "hint" not in data  # passing tables need no hint

    await _call(
        "fitnesse_debug_run_page", {"setup_only": True},
        (200, {"page": "FrontPage.T", "html": TEARDOWN_HTML}),
        (200, {"html": "<table><tr><td class=\"pass\">setup</td></tr></table>", "counts": {"right": 1}}),
    )
    data, _ = await _call("fitnesse_debug_run_wiki", {"wiki": "|greet|"}, wrong)
    assert "hint" not in data  # SetUp ran: a real failure


@pytest.mark.asyncio
async def test_run_page_with_tables_that_include_the_setup_counts_as_setup(debug_env):
    await _start()
    page = (200, {"page": "FrontPage.T", "html": TEARDOWN_HTML})
    ran = (200, {"html": "<table><tr><td class=\"pass\">setup</td></tr></table>"
                         "<table><tr><td class=\"fail\">own</td></tr></table>", "counts": {"right": 1, "wrong": 1}})
    data, _ = await _call("fitnesse_debug_run_page", {"tables": [0, 1]}, page, ran)
    wrong = (200, {"html": "<table><tr><td class=\"error\">greet</td></tr></table>", "counts": {"exceptions": 1}})
    data, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|greet|"}, wrong)
    assert fake.actions == ["runWiki"] and "hint" not in data  # SetUp ran with tables=[0, 1]
    data, fake = await _call("fitnesse_debug_run_page", {}, page, (200, {"html": "", "counts": {}}))
    assert data["skipped_setup"] == [0]  # and a whole-page run leaves it out


@pytest.mark.asyncio
async def test_run_page_with_tables_numbers_them_as_on_the_page(debug_env):
    await _start()
    page = (200, {"page": "FrontPage.T", "html": TEARDOWN_HTML})
    ran = (200, {"html": "<table><tr><td class=\"fail\">teardown</td></tr></table>"
                         "<table><tr><td class=\"pass\">own</td></tr></table>", "counts": {"right": 1, "wrong": 1}})
    data, _ = await _call("fitnesse_debug_run_page", {"tables": [2, 1]}, page, ran)
    assert data["tables"].startswith("Table 2:\n| [fail] teardown |") and "Table 1:\n| [pass] own |" in data["tables"]
    data, _ = await _call("fitnesse_debug_run_page", {"tables": [2, 1], "only_failures": True}, page, ran)
    assert data["tables"].startswith("Table 2:") and "Table 1" not in data["tables"]


@pytest.mark.asyncio
async def test_run_wiki_failing_on_a_page_without_setup_has_no_setup_hint(debug_env):
    await _start()
    wrong = (200, {"html": "<table><tr><td class=\"fail\">docker run</td></tr></table>", "counts": {"wrong": 1}})
    no_setup = (200, {"html": "<table><tr><td>own</td></tr></table>"})
    data, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|docker run|x|"}, wrong, no_setup)
    assert fake.actions == ["runWiki", "pageHtml"] and "hint" not in data
    data, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|docker run|x|"}, wrong)
    assert fake.actions == ["runWiki"] and "hint" not in data  # looked up once per test


@pytest.mark.asyncio
async def test_run_page_setup_only_excludes_tables(debug_env):
    await _start()
    with pytest.raises(ToolError, match="either setup_only"):
        await _call("fitnesse_debug_run_page", {"setup_only": True, "tables": [0]})


def test_debug_without_read_or_test_warns_at_startup(caplog):
    previous = {k: os.environ.get(k) for k in GATES}
    try:
        for toolsets, missing in (("read,debug", "test"), ("test,write,debug", "read"), ("write,debug", "read and test"),
                                  ("read,test,debug", None), ("all", None)):
            caplog.clear()
            with caplog.at_level("WARNING", logger="fitnesse_mcp"):
                _reload(FITNESSE_TOOLSETS=toolsets)
            warned = [r.getMessage() for r in caplog.records if "debug without" in r.getMessage()]
            if missing:
                assert len(warned) == 1 and f"debug without {missing}:" in warned[0], toolsets
            else:
                assert not warned, toolsets
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(my_server)


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


def _include(page, inner, cls="collapsible"):
    return (f'<div class="{cls}"><p class="title">Included page: <a class="edit" href="/{page}?edit">edit</a></p>'
            f'<div>{inner}</div></div>')


def _t(text):
    return f"<table><tr><td>{text}</td></tr></table>"


SETUP_NESTED_HTML = (
    _include("Lib.SetUp", _t("open browser") + _include("Lib.Defs", _t("define actions at")))
    + _include("Shared.Login", _t("login"))
    + _t("own")
    + _include("Lib.TearDown", _t("close"), "collapsible teardown")
)


def test_setup_is_the_setup_page_with_what_it_includes_not_a_plain_include():
    assert my_server._setup_indices(my_server._parse_tables(SETUP_NESTED_HTML)) == [0, 1]
    only_include = _include("Shared.Login", _t("login"))
    assert my_server._setup_indices(my_server._parse_tables(only_include)) == []
    assert my_server._setup_indices(my_server._parse_tables(_include("S.SuiteSetUp", _t("x")))) == [0]


@pytest.mark.asyncio
async def test_run_page_after_setup_only_leaves_the_setup_out_until_restart(debug_env):
    await _start()
    page = (200, {"page": "FrontPage.T", "html": SETUP_NESTED_HTML})
    ran = (200, {"html": "", "counts": {}})
    _, fake = await _call("fitnesse_debug_run_page", {"setup_only": True}, page, ran)
    assert fake.requests[1]["form"]["html"] == _t("open browser") + "\n" + _t("define actions at")

    data, fake = await _call("fitnesse_debug_run_page", {}, page, ran)
    assert fake.actions == ["pageHtml", "runTable"]
    assert data["skipped_setup"] == [0, 1]
    assert fake.requests[1]["form"]["html"] == "\n".join([_t("login"), _t("own"), _t("close")])

    data, fake = await _call("fitnesse_debug_run_page", {"restart": True},
                             (200, {"html": "", "counts": {}, "page": "FrontPage.T"}), page, ran)
    assert fake.actions == ["nextPage", "pageHtml", "runTable"]
    assert "skipped_setup" not in data
    assert fake.requests[2]["form"]["html"] == SETUP_NESTED_HTML  # the whole page again, SetUp included


SUITE_FRAME = (200, {"page": "S.T", "pages": [
    {"path": "S.SuiteSetUp", "runnable": True, "kind": "suiteSetUp"},
    {"path": "S.T", "runnable": True, "kind": "test"},
    {"path": "S.SuiteTearDown", "runnable": True, "kind": "suiteTearDown"},
]})
ROW = "<table><tr><td class=\"pass\">{}</td></tr></table>"


async def _start_with_frame():
    data, fake = await _call(
        "fitnesse_debug_start", {"page_path": "S.T"},
        (200, {"running": False}), (200, {"started": True, "page": "S.T", "session": "s1"}), SUITE_FRAME,
    )
    assert fake.actions == ["status", "start", "suitePages"]
    return data


@pytest.mark.asyncio
async def test_single_page_runs_suite_setup_and_teardown_like_a_test_run(debug_env):
    # pageHtml leaves SuiteSetUp/SuiteTearDown out: FitNesse runs them as pages of their own
    data = await _start_with_frame()
    assert data["suite_setup"] == "S.SuiteSetUp" and data["suite_teardown"] == "S.SuiteTearDown"

    data, fake = await _call(
        "fitnesse_debug_run_page", {"setup_only": True},
        (200, {"page": "S.SuiteSetUp", "html": "", "counts": {}}),  # nextPage
        (200, {"html": ROW.format("start browser")}),
        (200, {"html": ROW.format("start browser"), "counts": {"right": 1}}),
        (200, {"page": "S.T", "html": "", "counts": {}}),  # nextPage back
        (200, {"page": "S.T", "html": TEARDOWN_HTML}),
        (200, {"html": ROW.format("setup"), "counts": {"right": 1}}),
    )
    assert fake.actions == ["nextPage", "pageHtml", "runTable", "nextPage", "pageHtml", "runTable"]
    assert fake.requests[0]["form"]["page"] == "S.SuiteSetUp" and fake.requests[3]["form"]["page"] == "S.T"
    assert {fake.requests[i]["form"]["withSuiteSetUp"] for i in (1, 4)} == {"false"}  # mod #164 would embed them
    assert data["suite_setup"]["page"] == "S.SuiteSetUp" and "[pass] start browser" in data["suite_setup"]["tables"]
    assert data["page"] == "S.T" and "[pass] setup" in data["tables"]

    _, fake = await _call("fitnesse_debug_run_page", {"tables": [2]},
                          (200, {"page": "S.T", "html": TEARDOWN_HTML}), (200, {"html": "", "counts": {}}))
    assert fake.actions == ["pageHtml", "runTable"]  # once per session

    data, fake = await _call(
        "fitnesse_debug_end", {},
        (200, {"page": "S.SuiteTearDown", "html": ROW.format("page teardown"), "counts": {}}),  # nextPage
        (200, {"html": ROW.format("close browser")}),
        (200, {"html": ROW.format("close browser"), "counts": {"right": 1}}),
        (200, {"lines": [], "next": 0}),
        (200, {"ended": True, "html": ROW.format("suite end"), "counts": {}}),
    )
    assert fake.actions == ["nextPage", "pageHtml", "runTable", "log", "end"]
    assert data["teardown"]["page"] == "S.T" and "page teardown" in data["teardown"]["tables"]
    assert data["suite_teardown"]["page"] == "S.SuiteTearDown" and "close browser" in data["suite_teardown"]["tables"]
    assert "suite end" in data["suite_teardown"]["end"]["tables"]
    assert set(data["page_totals"]) >= {"S.SuiteSetUp", "S.T", "S.SuiteTearDown"}


@pytest.mark.asyncio
async def test_suite_setup_hint_and_abort_without_suite_teardown(debug_env):
    await _start_with_frame()
    data, fake = await _call("fitnesse_debug_run_wiki", {"wiki": "|greet|"},
                             (200, {"html": "<table><tr><td class=\"error\">greet</td></tr></table>", "counts": {"exceptions": 1}}))
    assert fake.actions == ["runWiki"] and "SuiteSetUp (S.SuiteSetUp) has not run" in data["hint"]
    data, fake = await _call("fitnesse_debug_end", {"abort": True}, (200, {"lines": [], "next": 0}), (200, {"ended": True}))
    assert fake.actions == ["log", "abort"] and "suite_teardown" not in data


@pytest.mark.asyncio
async def test_no_suite_teardown_unless_the_suite_setup_ran(debug_env):
    await _start_with_frame()
    _, fake = await _call("fitnesse_debug_end", {}, (200, {"lines": [], "next": 0}), (200, {"ended": True, "html": "", "counts": {}}))
    assert fake.actions == ["log", "end"]


@pytest.mark.asyncio
async def test_older_fork_without_suite_pages_for_a_single_page(debug_env):
    data, fake = await _call(
        "fitnesse_debug_start", {"page_path": "S.T"},
        (200, {"running": False}), (200, {"started": True, "page": "S.T", "session": "s1"}),
        (400, {"error": "unknownAction"}),
    )
    assert data["started"] and "suite_setup" not in data
    _, fake = await _call("fitnesse_debug_run_page", {}, (200, {"page": "S.T", "html": ""}), (200, {"html": "", "counts": {}}))
    assert fake.actions == ["pageHtml", "runTable"]
