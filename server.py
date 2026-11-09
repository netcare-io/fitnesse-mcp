# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT

import binascii
import difflib
from html import unescape as _unescape_html
import http.client
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Annotated, Literal, get_args
from urllib import parse, request
from urllib.error import HTTPError, URLError

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.transforms import Visibility
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import File, Image
from mcp.types import TextContent
from pydantic import Field

mcp = FastMCP("Fitnesse MCP Server")
logger = logging.getLogger("fitnesse_mcp")

MAX_BYTES = int(
    os.getenv("FITNESSE_MAX_RESPONSE_BYTES", str(1024 * 1024))
)  # default 1 MB
MAX_TRANSFER_BYTES = int(
    os.getenv("FITNESSE_FILES_MAX_TRANSFER_BYTES", str(10 * 1024 * 1024))
)  # default 10 MB
# Read timeout for fitnesse_run_test/fitnesse_run_suite; 0 or less disables it.
_run_timeout_env = float(os.getenv("FITNESSE_RUN_TIMEOUT_SECONDS", "120"))
RUN_TIMEOUT_SECONDS = _run_timeout_env if _run_timeout_env > 0 else None

# Type aliases with per-parameter descriptions for model-facing schemas.
type WikiPath = Annotated[
    str,
    Field(
        description="Dotted wiki page path, e.g. 'FrontPage.MySuite.MyTest'. No leading slash."
    ),
]
type FilesPath = Annotated[
    str,
    Field(
        description="Directory in the FitNesse files section, e.g. 'files/images'. Has to start with the root set in FITNESSE_FILES_ROOT (usually 'files'). No leading slash."
    ),
]
type PageName = Annotated[
    str,
    Field(description="Leaf page name only (no dots or slashes), e.g. 'MyNewPage'."),
]
type PageContent = Annotated[
    str, Field(description="Raw FitNesse wiki markup content for the page.")
]
type PageType = Annotated[
    Literal["Static", "Test", "Suite"],
    Field(description="Page type."),
]
type TagList = Annotated[str | None, Field(description="Comma-separated tags.")]
type RunFormat = Annotated[
    Literal["xml", "junit", "text"],
    Field(description="xml: full report; junit: simple jUnit XML; text: plain text summary."),
]


type _ANY_TAG = Annotated[str | None, Field(description="Only tests with any of these comma-separated tags.")]
type _ALL_TAGS = Annotated[str | None, Field(description="Only tests with all of these comma-separated tags.")]
type _NO_TAG = Annotated[str | None, Field(description="Leave out tests with any of these comma-separated tags.")]
type _FIRST_TEST = Annotated[str | None, Field(description="Start at this test page (in run order) and leave out those before it.")]
type _VERSION = Annotated[str, Field(description="A version id from fitnesse_page_versions, e.g. 'jane-20261109143000'.")]
type _NO_HISTORY = Annotated[bool, Field(description="Do not store the run in the test history.")]
type _NO_CHUNK = Annotated[bool, Field(description="Have FitNesse send the report in one piece; does not change it.")]
type _RUN_VARIABLES = Annotated[dict[str, str] | None, Field(description="Page variables for this run, e.g. {'BASE_URL': 'https://test.example'}; they override those defined on the pages.")]


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


# FITNESSE_TOOLSETS combines two kinds of values (all = every mode; areas are always named):
# - modes: a tool is visible only when its mode is listed. read/write/test are named after
#   FitNesse's secure-read / secure-write / secure-test page attributes.
# - areas: tools beyond the standard ones, each still filtered by the modes. complete adds
#   the lower-traffic tools, debug the KDTA debug tools (fitnesse_debug_*, fitnesse_table_*).
type Mode = Literal["read", "write", "test"]
MODES: tuple[str, ...] = get_args(Mode.__value__)
type Area = Literal["complete", "debug"]
AREAS: tuple[str, ...] = get_args(Area.__value__)
TOOLSETS: tuple[str, ...] = MODES + AREAS
# Areas stay opt-in even with all: debug needs netcare's FitNesse fork, complete only adds
# rarely needed tools. all,debug,complete is everything.
ALL_TOOLSETS = frozenset(MODES)
def _toolsets_from_env() -> frozenset[str]:
    """Toolsets from FITNESSE_TOOLSETS (comma-separated or 'all'); required, so nothing is granted by accident."""
    raw = os.getenv("FITNESSE_TOOLSETS", "").strip()
    if not raw:
        raise RuntimeError(
            "FITNESSE_TOOLSETS is required: 'all' or a comma-separated subset of "
            f"{', '.join(TOOLSETS)} (e.g. 'read', 'read,test,debug', or 'all,debug,complete'). It replaces "
            "FITNESSE_READONLY and FITNESSE_COMPLETE_TOOLSET."
        )
    chosen = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if "all" in chosen:
        chosen = (chosen - {"all"}) | ALL_TOOLSETS
    unknown = chosen - set(TOOLSETS)
    if unknown or not chosen:
        raise RuntimeError(
            f"FITNESSE_TOOLSETS={raw!r}: expected 'all' or a comma-separated subset of "
            f"{', '.join(TOOLSETS)} (e.g. 'read', 'read,test,debug', or 'all,debug,complete')."
        )
    if not chosen & set(MODES):
        # An area alone would show nothing: its tools are filtered by the modes too.
        raise RuntimeError(
            f"FITNESSE_TOOLSETS={raw!r}: {' and '.join(sorted(chosen))} need at least one of "
            f"{', '.join(MODES)}, e.g. 'read,{','.join(sorted(chosen))}'."
        )
    return frozenset(chosen)


FITNESSE_TOOLSETS = _toolsets_from_env()

# Extra low-traffic/read-heavy tools trimmed from the default set; opt in with FITNESSE_TOOLSETS=...,complete.
COMPLETE_TOOLSET = "complete" in FITNESSE_TOOLSETS

# Whose FitNesse account a call uses. env: FITNESSE_USERNAME/FITNESSE_PASSWORD for every caller.
# headers: each HTTP request brings its own (HEADER_USERNAME/HEADER_PASSWORD); the env pair is
# ignored, so a shared server never falls back to its own account.
type AuthMode = Literal["env", "headers"]
AUTH_MODES: tuple[str, ...] = get_args(AuthMode.__value__)
HEADER_USERNAME = "X-FitNesse-Username"
HEADER_PASSWORD = "X-FitNesse-Password"


def _auth_mode_from_env() -> str:
    raw = os.getenv("FITNESSE_AUTH_MODE", "").strip().lower() or "env"
    if raw not in AUTH_MODES:
        raise RuntimeError(f"FITNESSE_AUTH_MODE={raw!r}: expected one of {', '.join(AUTH_MODES)}.")
    return raw


FITNESSE_AUTH_MODE = _auth_mode_from_env()


def _make_timeout(default: float):
    def factory() -> float:
        return default

    return Depends(factory)


def _make_run_timeout():
    def factory() -> float | None:
        return RUN_TIMEOUT_SECONDS

    return Depends(factory)


def _credentials() -> tuple[str | None, str | None]:
    """FitNesse username and password for the current call (see FITNESSE_AUTH_MODE)."""
    if FITNESSE_AUTH_MODE == "env":
        return os.getenv("FITNESSE_USERNAME"), os.getenv("FITNESSE_PASSWORD")
    headers = get_http_headers()  # lower-case names; {} outside an HTTP request
    username = headers.get(HEADER_USERNAME.lower())
    password = headers.get(HEADER_PASSWORD.lower())
    if not username or not password:
        raise ToolError(
            f"FITNESSE_AUTH_MODE=headers: send your FitNesse login in the {HEADER_USERNAME} and "
            f"{HEADER_PASSWORD} headers of every request (HTTP transport only)."
        )
    return username, password


HEADER_BASE_URL = "X-FitNesse-Url"


def _base_url() -> str:
    """FitNesse instance for the current call: the X-FitNesse-Url header (HTTP transport), else
    FITNESSE_BASE_URL. Any http(s) URL is accepted; see the README on what that allows callers."""
    url = (get_http_headers().get(HEADER_BASE_URL.lower()) or "").strip()
    if not url:
        return os.getenv("FITNESSE_BASE_URL", "http://localhost:8080")
    parts = parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise ToolError(f"{HEADER_BASE_URL} must be an http(s) URL of a FitNesse, e.g. https://fitnesse.example.com")
    return url.rstrip("/")


def _apply_basic_auth(
    req: request.Request, username: str | None, password: str | None
) -> None:
    if not username or not password:
        return

    token = b64encode(f"{username}:{password}".encode()).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")


def _encode_query(
    params: Mapping[str, str | None] | list[tuple[str, str | None]] | None = None,
) -> str:
    if not params:
        return ""

    # FitNesse supports flag inputs like ?test and ?Recursive.
    items = params.items() if isinstance(params, Mapping) else params
    encoded: list[str] = []
    for key, value in items:
        key_q = parse.quote_plus(str(key))
        if value is None:
            encoded.append(key_q)
        else:
            encoded.append(f"{key_q}={parse.quote_plus(str(value))}")
    return "&".join(encoded)


_RESERVED_PARAMS = {"responder", "confirmed", "format"}


def _merge_extra(
    params: dict[str, str | None], extra: dict[str, str] | None
) -> dict[str, str | None]:
    if not extra:
        return params
    bad = _RESERVED_PARAMS & extra.keys()
    if bad:
        raise ToolError(
            f"Reserved query parameters cannot be overridden: {sorted(bad)}"
        )
    return {**params, **extra}


def _files_root_parts() -> list[str]:
    """The files-section resource root as configured for this FitNesse instance."""
    root = os.environ.get("FITNESSE_FILES_ROOT", "").strip().strip("/")
    return [p for p in root.split("/") if p and p != "."]


def _files_resource(files_path: str) -> str:
    """Normalize a files-section resource path, rejecting anything outside FITNESSE_FILES_ROOT."""
    root_parts = _files_root_parts()
    parts = [p for p in files_path.strip().strip("/").split("/") if p not in {"", "."}]
    if not root_parts or ".." in parts or parts[: len(root_parts)] != root_parts:
        root_display = "/".join(root_parts) or "<FITNESSE_FILES_ROOT unset>"
        raise ToolError(f"files_path must stay below '{root_display}/': {files_path!r}")
    return "/".join(parts)


def _files_name(name: str, field: str) -> str:
    """Require a plain file or directory name — no separators or traversal."""
    if not name or name in {".", ".."} or set(name) & set('/\\\x00\r\n"'):
        raise ToolError(f"{field} must be a plain name without separators: {name!r}")
    return name


def _build_url(
    base_url: str,
    path: str,
    params: Mapping[str, str | None] | list[tuple[str, str | None]] | None = None,
) -> str:
    base = base_url.rstrip("/")
    stripped = path.lstrip("/")
    # Reject null bytes, query/fragment injection, and path traversal.
    if "\x00" in stripped or "?" in stripped or "#" in stripped or ".." in stripped:
        raise ToolError(f"Invalid path: {stripped!r}")
    encoded_path = parse.quote(stripped, safe="./")
    normalized_path = "/" + encoded_path if encoded_path else "/"
    if not params:
        return f"{base}{normalized_path}"
    query = _encode_query(params)
    return f"{base}{normalized_path}?{query}"


def _read_body(response, limit: int) -> bytes:
    """Up to limit bytes of the body. http.client's read(n) may return fewer bytes than asked for
    before the end of the body, so read until EOF or the limit."""
    chunks, size = [], 0
    while size < limit:
        chunk = response.read(limit - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _missing_bytes(headers, body: bytes, limit: int) -> str | None:
    """'closed after N of M bytes' if the connection ended before Content-Length (and the limit).
    Seen with FitNesse behind Docker's host.docker.internal: a few large answers end early."""
    try:
        length = int(headers.get("Content-Length") or "") if headers is not None else None
    except ValueError:
        length = None
    if length is None or len(body) >= min(length, limit):
        return None
    return f"FitNesse closed the connection after {len(body)} of {length} bytes"


_READ_ATTEMPTS = 3  # for answers that end early; only requests that change nothing are repeated
# GET responders that only read. Others act on a GET (test, suite, deletePage, purgeHistory, …)
# and are never repeated: a run whose answer broke off may still have happened.
_READ_ONLY_RESPONDERS = {
    None, "compareHistory", "compareVersions", "edit", "executionLog", "files", "getPage", "names",
    "overview", "packet", "pageData", "pageHistory", "properties", "raw", "rss", "search",
    "searchForm", "searchProperties", "testHistory", "variables", "versions", "viewVersion",
    "whereUsed",
}
_CLOSED = (http.client.HTTPException, ConnectionError)  # e.g. BadStatusLine: closed before the status line


def _closed_error(exc: BaseException) -> str:
    text = str(exc).strip(" '\"")  # BadStatusLine('') prints as ''
    detail = f": {text}" if text else ""
    return f"FitNesse closed the connection without a complete answer ({type(exc).__name__}{detail})"


def _responder(params) -> str | None:
    items = params.items() if isinstance(params, Mapping) else (params or [])
    return next((value for key, value in items if key == "responder"), None)


def _retrying(once):
    """Repeat a read-only GET whose answer ended early (result '_incomplete'); report it after the
    last try. A GET that acts (a test run) is not repeated: its error says it may have run."""
    def call(method: str, path: str, *args, **kwargs) -> dict:
        params = kwargs.get("params", args[0] if args else None)
        reads = method.upper() in ("GET", "HEAD") and _responder(params) in _READ_ONLY_RESPONDERS
        attempts = _READ_ATTEMPTS if reads else 1
        for attempt in range(1, attempts + 1):
            result = once(method, path, *args, **kwargs)
            if not result.pop("_incomplete", False):
                return result
            logger.warning("%s (%s, attempt %d of %d)", result["error"], result["url"], attempt, attempts)
        result["error"] += (
            f"; tried {attempts} times" if attempts > 1 else
            "; the request may have taken effect anyway (a test or suite may have run: check "
            "fitnesse_get_page_history before running it again)"
        )
        return result
    call.__wrapped__ = once
    return call


def _page_change(result: dict, done: str) -> dict:
    """The short answer of a tool that changes pages: FitNesse answers with a whole page (15-50k characters)."""
    out = {"ok": result.get("ok", False), "status": result.get("status")}
    if result.get("ok"):
        out["message"] = done
        return out
    out["error"] = result.get("error", "")
    body = result.get("body")
    if isinstance(body, str) and body:
        out["message"] = _plain_text(re.sub(r"(?s)<(script|style)\b.*?</\1>", "", body))[:300]
    return out


@_retrying
def _request(
    method: str,
    path: str,
    params: Mapping[str, str | None] | list[tuple[str, str | None]] | None = None,
    body: str | bytes | None = None,
    content_type: str | None = None,
    timeout_seconds: float | None = 30.0,
    username: str | None = None,
    password: str | None = None,
    base_url: str | None = None,
) -> dict:
    if base_url is None:
        base_url = _base_url()
    if username is None and password is None:
        username, password = _credentials()
    url = _build_url(base_url=base_url, path=path, params=params)
    data = body.encode() if isinstance(body, str) else body
    req = request.Request(url=url, data=data, method=method.upper())
    _apply_basic_auth(req=req, username=username, password=password)

    if content_type:
        req.add_header("Content-Type", content_type)

    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw_bytes = _read_body(response, MAX_BYTES + 1)
            missing = _missing_bytes(response.headers, raw_bytes, MAX_BYTES + 1)
            if missing:
                return {"ok": False, "status": response.status, "url": url, "error": missing, "_incomplete": True}
            truncated = len(raw_bytes) > MAX_BYTES
            if truncated:
                logger.warning(
                    "Response from %s truncated to FITNESSE_MAX_RESPONSE_BYTES=%d",
                    url,
                    MAX_BYTES,
                )
            raw = raw_bytes[:MAX_BYTES].decode("utf-8", errors="replace")
            content_type_header = response.headers.get("Content-Type", "")
            payload: str | dict | list = raw
            if "application/json" in content_type_header:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = raw

            return {
                "ok": True,
                "status": response.status,
                "url": url,
                "content_type": content_type_header,
                "truncated": truncated,
                "body": payload,
            }
    except HTTPError as exc:
        error_body = _read_body(exc, MAX_BYTES).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "url": url,
            "error": f"HTTP error: {exc.reason}",
            "body": error_body,
        }
    except TimeoutError:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "error": "Timeout reading response",
            "body": None,
        }
    except URLError as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "error": f"Connection error: {exc.reason}",
            "body": None,
        }
    except _CLOSED as exc:
        return {"ok": False, "status": None, "url": url, "error": _closed_error(exc), "body": None,
                "_incomplete": True}


@_retrying
def _request_bytes(
    method: str,
    path: str,
    params: Mapping[str, str | None] | list[tuple[str, str | None]] | None = None,
    timeout_seconds: float | None = 30.0,
    username: str | None = None,
    password: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Like _request, but returns the raw response body bytes undecoded."""
    if base_url is None:
        base_url = _base_url()
    if username is None and password is None:
        username, password = _credentials()
    url = _build_url(base_url=base_url, path=path, params=params)
    req = request.Request(url=url, method=method.upper())
    _apply_basic_auth(req=req, username=username, password=password)

    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw_bytes = _read_body(response, MAX_TRANSFER_BYTES + 1)
            missing = _missing_bytes(response.headers, raw_bytes, MAX_TRANSFER_BYTES + 1)
            if missing:
                return {"ok": False, "status": response.status, "url": url, "error": missing, "_incomplete": True}
            truncated = len(raw_bytes) > MAX_TRANSFER_BYTES
            if truncated:
                logger.warning(
                    "Response from %s truncated to FITNESSE_FILES_MAX_TRANSFER_BYTES=%d",
                    url,
                    MAX_TRANSFER_BYTES,
                )
            return {
                "ok": True,
                "status": response.status,
                "url": url,
                "content_type": response.headers.get("Content-Type", ""),
                "truncated": truncated,
                "body": raw_bytes[:MAX_TRANSFER_BYTES],
            }
    except HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "url": url,
            "error": f"HTTP error: {exc.reason}",
            "body": _read_body(exc, MAX_BYTES).decode("utf-8", errors="replace"),
        }
    except TimeoutError:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "error": "Timeout reading response",
            "body": None,
        }
    except URLError as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "error": f"Connection error: {exc.reason}",
            "body": None,
        }
    except _CLOSED as exc:
        return {"ok": False, "status": None, "url": url, "error": _closed_error(exc), "body": None,
                "_incomplete": True}


def _build_multipart_form_data(
    fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes
) -> tuple[bytes, str]:
    boundary = f"----fitnesse-mcp-{uuid.uuid4().hex}"
    lines: list[bytes] = []

    for key, value in fields.items():
        lines.extend(
            [
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="{key}"'.encode(),
                b"",
                value.encode(),
            ]
        )

    lines.extend(
        [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(),
            b"Content-Type: application/octet-stream",
            b"",
            file_bytes,
            f"--{boundary}--".encode(),
            b"",
        ]
    )

    body = b"\r\n".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


# Tools below are ordered to match FitNesse RestfulServices responder order.


@mcp.tool(
    name="fitnesse_add_child_page",
    description=(
        'Creates a page below parent_page_path with the given wiki text. page_type makes it a '
        'Test page (can run), a Suite (runs its child pages) or Static; without it FitNesse '
        "decides by the name (e.g. a name starting or ending with Test). Result: ok, status and a short "
        'message; read the page back with fitnesse_get_page_data.'
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False},
)
def fitnesse_add_child_page(
    parent_page_path: WikiPath,
    page_name: PageName,
    page_content: PageContent = "",
    page_template: Annotated[str | None, Field(description="Path of a template page whose content the new page starts with, e.g. '.TemplateLibrary.TestPage'; page_content then is ignored.")] = None,
    page_type: PageType | None = None,
    help_text: Annotated[str | None, Field(description="Short description, shown next to the page's link in listings.")] = None,
    suites: Annotated[str | None, Field(description="Comma-separated tags for the new page.")] = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Create a child page using responder=addChild."""
    # POST like the browser's form (FitNesse answers 303 to the new page): a long page_content
    # does not fit into a URL.
    form: dict[str, str] = {"responder": "addChild", "pageName": page_name, "pageContent": page_content}
    if page_template:
        form["pageTemplate"] = page_template
    if page_type:
        form["pageType"] = page_type
    if help_text:
        form["helpText"] = help_text
    if suites:
        form["suites"] = suites
    result = _request("POST", parent_page_path, body=parse.urlencode(form),
                      content_type="application/x-www-form-urlencoded", timeout_seconds=timeout_seconds)
    return _page_change(result, f"Created {parent_page_path}.{page_name}. Read it with fitnesse_get_page_data.")


if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_create_dir",
        description=(
            'Creates the directory dirname in a directory of the files section.'
        ),
        tags={"fitnesse", "rest", "files", "write"},
        annotations={"destructiveHint": False, "idempotentHint": True},
    )
    def fitnesse_create_dir(
        files_path: FilesPath,
        dirname: str,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Create a new directory in the files section via responder=createDir."""
        return _request(
            "GET",
            _files_resource(files_path),
            params={
                "responder": "createDir",
                "dirname": _files_name(dirname, "dirname"),
            },
            timeout_seconds=timeout_seconds,
        )


@mcp.tool(
    name="fitnesse_delete_page",
    description=(
        'Deletes the page together with all its child pages. Only on request: there is no '
        'undo.'
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": True},
)
def fitnesse_delete_page(
    page_path: WikiPath,
    delete_versions: Annotated[
        bool,
        Field(description="Also delete the page's saved versions (FitNesse 20261101 or later; older releases ignore it)."),
    ] = False,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Delete a page via responder=deletePage."""
    params: dict[str, str | None] = {"responder": "deletePage", "confirmed": "yes"}
    if delete_versions:
        params["deleteVersions"] = "yes"
    result = _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    return _page_change(result, f"Deleted {page_path} with its child pages.")


if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_delete_file",
        description=(
            'Deletes a file, or a directory with everything in it, from a directory of the files '
            'section. No undo.'
        ),
        tags={"fitnesse", "rest", "files", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_delete_file(
        files_path: FilesPath,
        filename: str,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Delete a file via responder=deleteFile."""
        return _request(
            "GET",
            _files_resource(files_path),
            params={
                "responder": "deleteFile",
                "filename": _files_name(filename, "filename"),
            },
            timeout_seconds=timeout_seconds,
        )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_edit_page",
        description="Returns the edit screen for a wiki page and supports redirectToReferer, redirectAction, and nonExistent options.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_edit_page(
        page_path: WikiPath,
        redirect_to_referer: bool = False,
        redirect_action: str | None = None,
        non_existent: bool = False,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Open edit responder with optional redirect/nonExistent options."""
        params: dict[str, str | None] = {"responder": "edit"}
        if redirect_to_referer:
            params["redirectToReferer"] = None
        if redirect_action:
            params["redirectAction"] = redirect_action
        if non_existent:
            params["nonExistent"] = None
        return _request(
            "GET", page_path, params=params, timeout_seconds=timeout_seconds
        )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_search_properties",
        description=(
            "Finds pages by their properties (responder=searchProperties; the deprecated "
            "executeSearchProperties is the same responder). Every criterion left out is ignored. "
            "page_types: pages of any of these types. suites: pages with any of these tags. actions "
            "/ security / special: each listed attribute must be set and each unlisted one of that "
            "group must not be, so list exactly the combination you want ('Any' ignores the group). "
            "special covers SetUp/TearDown pages: listed kinds are included, unlisted excluded. "
            "prune=true finds only pages marked as skipped. Searches page_path and its children "
            "unless search_scope names an ancestor or 'root'. Navigation actions (RecentChanges, "
            "Files, Search) are only matched by FitNesse 20261101 or later. Result: matches (page, type, "
            "tags, last_modified) and count."
        ),
        tags={"fitnesse", "rest", "search", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_search_properties(
        page_path: WikiPath = "",
        page_types: list[Literal["Static", "Test", "Suite"]] | None = None,
        suites: TagList = None,
        actions: list[
            Literal[
                "Any", "Edit", "Versions", "Properties", "Refactor", "WhereUsed",
                "RecentChanges", "Files", "Search",
            ]
        ] | None = None,
        security: list[Literal["Any", "secure-read", "secure-write", "secure-test"]] | None = None,
        special: list[Literal["Any", "SetUp", "TearDown"]] | None = None,
        prune: bool = False,
        search_scope: Annotated[
            str | None,
            Field(description="Ancestor page to search below, or 'root' for the whole wiki."),
        ] = None,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """Search by properties via responder=searchProperties."""
        # without searchType netcare's FitNesse answers with the search form only
        params: dict[str, str | None] = {"responder": "searchProperties", "searchType": "Search Properties"}
        if page_types:
            # "PageType": the only name FitNesse 20251025 reads; 20261101 still accepts it.
            params["PageType"] = ",".join(page_types)
        if suites:
            params["Suites"] = suites
        for key, values in (("Action", actions), ("Security", security), ("Special", special)):
            if values is not None:
                params[key] = "Any" if "Any" in values else ",".join(values)
        if prune:
            params["Prune"] = None
        if search_scope:
            params["searchScope"] = search_scope
        return _search_matches(_request("GET", page_path, params=params, timeout_seconds=timeout_seconds))

if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_list_files",
        description=(
            'Lists a directory of the files section (uploads, screenshots, reports) as JSON '
            '(body): its files and subdirectories.'
        ),
        tags={"fitnesse", "rest", "files", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_list_files(
        files_path: FilesPath = "files",
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Display a file directory via responder=files."""
        return _request(
            "GET",
            _files_resource(files_path),
            params={"responder": "files", "format": "json"},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(
        name="fitnesse_download_file",
        description=(
            'Reads a file of the files section: an image comes back as an image, anything else as '
            'a resource with its content.'
        ),
        tags={"fitnesse", "rest", "files", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_download_file(
        files_path: FilesPath,
        filename: str,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> ToolResult:
        """Download a file from the files section by GETing its direct resource path."""
        resource = _files_resource(files_path)
        filename = _files_name(filename, "filename")

        result = _request_bytes(
            "GET",
            f"{resource}/{filename}",
            timeout_seconds=timeout_seconds,
        )
        if not result["ok"]:
            return ToolResult(structured_content=result)
        if result["truncated"]:
            return ToolResult(
                structured_content={
                    "ok": False,
                    "error": f"File exceeds transfer limit of {MAX_TRANSFER_BYTES} bytes",
                }
            )

        data = result.pop("body")
        content_type = (result.get("content_type") or "").split(";")[0].strip()
        if not content_type or content_type == "application/octet-stream":
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        result["content_type"] = content_type
        result["bytes"] = len(data)

        if content_type.startswith("image/"):
            block = Image(data=data, format=content_type.split("/", 1)[1]).to_image_content()
        else:
            fmt = content_type.split("/", 1)[1] if "/" in content_type else None
            block = File(data=data, format=fmt, name=filename).to_resource_content(mime_type=content_type)

        return ToolResult(content=[block], structured_content=result)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_page",
        description="Views the selected page, supports dontCreatePage, and accepts key value markup variables.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_page(
        page_path: WikiPath,
        dont_create_page: bool = False,
        variables: dict[str, str] | None = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """View a page via responder=getPage with optional markup variables."""
        params: dict[str, str | None] = {"responder": "getPage"}
        if dont_create_page:
            params["dontCreatePage"] = None
        params = _merge_extra(params, variables)
        return _request(
            "GET", page_path, params=params, timeout_seconds=timeout_seconds
        )


def _import_pages(
    page_path: WikiPath,
    remote_url: str,
    remote_username: str | None,
    remote_password: str | None,
    auto_update: bool,
    import_and_view: bool,
    timeout_seconds: float,
) -> dict:
    params: dict[str, str | None] = {
        "responder": "importAndView" if import_and_view else "import",
        "remoteUrl": remote_url,
    }
    if remote_username:
        params["remoteUsername"] = remote_username
    if remote_password:
        params["remotePassword"] = remote_password
    if auto_update:
        params["autoUpdate"] = None
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_import_pages",
        description=(
            'Imports the pages below remote_url, a page of another FitNesse, as children of '
            'page_path. auto_update=true imports them again whenever the page is viewed.'
        ),
        tags={"fitnesse", "rest", "pages", "write"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_import_pages(
        page_path: WikiPath,
        remote_url: str,
        remote_username: str | None = None,
        remote_password: str | None = None,
        auto_update: bool = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        return _import_pages(
            page_path,
            remote_url,
            remote_username,
            remote_password,
            auto_update,
            False,
            timeout_seconds,
        )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_import_and_view",
        description=(
            "As fitnesse_import_pages, but answers with the imported page's view."
        ),
        tags={"fitnesse", "rest", "pages", "write"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_import_and_view(
        page_path: WikiPath,
        remote_url: str,
        remote_username: str | None = None,
        remote_password: str | None = None,
        auto_update: bool = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        return _import_pages(
            page_path,
            remote_url,
            remote_username,
            remote_password,
            auto_update,
            True,
            timeout_seconds,
        )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_instruction",
        description="Displays Slim instructions for a suite of Slim tests for analysis or low level debugging.",
        tags={"fitnesse", "rest", "tests", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_instruction(
        page_path: WikiPath,
        variables: dict[str, str] | None = None,
        timeout_seconds: float = _make_timeout(120.0),
    ) -> dict:
        """Get Slim instructions via responder=instruction."""
        return _request(
            "GET",
            page_path,
            params=_merge_extra({"responder": "instruction"}, variables),
            timeout_seconds=timeout_seconds,
        )


@mcp.tool(
    name="fitnesse_move_page",
    description=(
        'Moves the page with its child pages below another parent page; its name stays. To '
        'rename it, use fitnesse_rename_page.'
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False},
)
def fitnesse_move_page(
    page_path: WikiPath,
    new_location: Annotated[str, Field(description="Path of the new parent page, e.g. 'FrontPage.Archive'; empty for the top level.")],
    refactor_references: Annotated[
        bool, Field(description="Also update references to the moved page.")
    ] = False,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Move a page under another parent via responder=movePage."""
    params: dict[str, str | None] = {"responder": "movePage", "newLocation": new_location}
    if refactor_references:
        params["refactorReferences"] = None
    result = _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    return _page_change(result, f"Moved {page_path} below {new_location or 'the top level'}.")


@mcp.tool(
    name="fitnesse_list_names",
    description=(
        'Lists the child pages of page_path (default: the top level) as a list of names '
        '(body). recursive=true lists the whole subtree as paths relative to page_path. Use it '
        'to find pages; fitnesse_search finds them by content.'
    ),
    tags={"fitnesse", "rest", "pages", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_list_names(
    page_path: WikiPath = "",
    recursive: Annotated[bool, Field(description="All pages below page_path, not only its children.")] = False,
    leaf_only: Annotated[bool, Field(description="Only pages without children.")] = False,
    show_tags: Annotated[bool, Field(description="Append each page's tags to its name.")] = False,
    show_child_count: Annotated[bool, Field(description="Append each page's number of children to its name, e.g. 'MySuite 4'.")] = False,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """List page names using responder=names."""
    params: dict[str, str | None] = {"responder": "names", "format": "json"}
    if recursive:
        params["Recursive"] = None
    if leaf_only:
        params["LeafOnly"] = None
    if show_tags:
        params["ShowTags"] = None
    if show_child_count:
        params["ShowChildCount"] = None
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_new_page_form",
        description="Returns the new page form similar to edit, with optional pageTemplate and pageType.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_new_page_form(
        page_path: WikiPath,
        page_template: str | None = None,
        page_type: PageType | None = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Open new-page form via responder=new."""
        params: dict[str, str | None] = {"responder": "new"}
        if page_template:
            params["pageTemplate"] = page_template
        if page_type:
            params["pageType"] = page_type
        return _request(
            "GET", page_path, params=params, timeout_seconds=timeout_seconds
        )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_packet",
        description="Returns a JSON packet containing all tables on a page and optionally wraps output using jsonp.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_packet(
        page_path: WikiPath,
        jsonp: str | None = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get table packet JSON via responder=packet."""
        params: dict[str, str | None] = {"responder": "packet"}
        if jsonp:
            params["jsonp"] = jsonp
        return _request(
            "GET", page_path, params=params, timeout_seconds=timeout_seconds
        )


_EDIT_FORM_FIELD = re.compile(r'<input type="hidden" name="(editTime|ticketId)" value="(-?\d+)"')


def _edit_session(page_path: str, timeout_seconds: float) -> dict[str, str | None]:
    """editTime/ticketId from the edit form (read permission suffices); None when unavailable."""
    form = _request("GET", page_path, params={"responder": "edit"}, timeout_seconds=timeout_seconds)
    body = form.get("body")
    fields = dict(_EDIT_FORM_FIELD.findall(body)) if form.get("ok") and isinstance(body, str) else {}
    return {"edit_time": fields.get("editTime"), "ticket_id": fields.get("ticketId")}


@mcp.tool(
    name="fitnesse_get_page_data",
    description=(
        'Returns the wiki text of a page (body), plus edit_time and ticket_id. Pass both to '
        'fitnesse_save_page_content when saving a change based on this text: if someone else '
        'saved the page in between, the save then fails instead of overwriting their change. '
        'Text of included pages (SetUp, !include) is not part of it.'
    ),
    tags={"fitnesse", "rest", "pages", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_page_data(
    page_path: WikiPath,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Get raw wiki text via responder=pageData, with the edit form's editTime/ticketId."""
    # editTime first: a save between the two requests then counts as a conflict.
    session = _edit_session(page_path, timeout_seconds)
    result = _request(
        "GET",
        page_path,
        params={"responder": "pageData"},
        timeout_seconds=timeout_seconds,
    )
    if result.get("ok"):
        result.update(session)
    return result


@mcp.tool(
    name="fitnesse_get_page_history",
    description=(
        "Test history of a page, condensed. Without result_date: the page's runs (runs: date, "
        "result_date, pass, fail). With result_date (yyyyMMddHHmmss from runs, or 'latest'): that "
        "run. For a suite: final_counts and pages (page, outcome, counts, result_date; failed_pages "
        "lists the red ones): call again with a failing page and its result_date. For a test page: "
        "outcome, counts and tables, only the tables with a [fail] or [error] cell, cells prefixed "
        "[pass]/[fail]/[error]/[ignore]. With compare_to (a second result_date; result_date then "
        "defaults to 'latest'): what differs between the two runs. Test page: differences, the "
        "tables whose rows are not the same in both, each as a diff (+ lines are from run, - lines "
        "from compared_to). Suite: the pages whose outcome differs. raw=true returns FitNesse's "
        "XML instead."
    ),
    tags={"fitnesse", "rest", "history", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_page_history(
    page_path: WikiPath,
    result_date: Annotated[str | None, Field(description="A run: result_date from runs (yyyyMMddHHmmss), or 'latest'.")] = None,
    compare_to: Annotated[str | None, Field(description="result_date of a second run to compare the first with.")] = None,
    include_command: Annotated[bool, Field(description="Also return the command line that started the test process.")] = False,
    include_stdout: Annotated[bool, Field(description="Also return the test process's output (can be very large).")] = False,
    include_stderr: Annotated[bool, Field(description="Also return the test process's error output.")] = False,
    raw: Annotated[bool, Field(description="Return FitNesse's XML (body) instead of the condensed result.")] = False,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Get page history via responder=pageHistory.

    The executionLog's command, stdOut and stdErr are dropped unless requested;
    stdOut alone can be megabytes and break MCP clients.
    """
    if compare_to:
        return _compare_runs(page_path, result_date or "latest", compare_to, timeout_seconds)
    params: dict[str, str | None] = {"responder": "pageHistory", "format": "xml"}
    if result_date:
        params["resultDate"] = result_date
    result = _request_bytes("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    if not result["ok"]:
        return result
    xml_bytes: bytes = result["body"]
    if result["truncated"]:
        logger.warning("Page history from %s not filtered: response was truncated", result["url"])
    else:
        xml_bytes = _strip_execution_log(
            xml_bytes,
            {
                tag
                for tag, keep in (
                    ("command", include_command),
                    ("stdOut", include_stdout),
                    ("stdErr", include_stderr),
                )
                if not keep
            },
        )
        summary = None if raw else _history_summary(xml_bytes)
        if summary is not None:
            del result["body"]
            result.update(summary)
            return result
    if len(xml_bytes) > MAX_BYTES:
        logger.warning(
            "Page history from %s is %d bytes after filtering, truncated to "
            "FITNESSE_MAX_RESPONSE_BYTES=%d",
            result["url"],
            len(xml_bytes),
            MAX_BYTES,
        )
        result["truncated"] = True
    result["body"] = xml_bytes[:MAX_BYTES].decode("utf-8", errors="replace")
    return result


_MAX_RUN_DIFFERENCES = 10


def _stored_run(page_path: str, result_date: str, timeout_seconds: float) -> dict:
    """One stored run for comparing: kind ('suite' or 'test'), head (what to show of it), and
    pages ({page: entry}) or tables ([(number, text)])."""
    result = _request_bytes("GET", page_path, timeout_seconds=timeout_seconds,
                            params={"responder": "pageHistory", "format": "xml", "resultDate": result_date})
    try:
        root = ET.fromstring(result["body"]) if result.get("ok") and not result.get("truncated") else None
    except ET.ParseError:
        root = None
    if root is None or root.tag not in ("suiteResults", "testResults"):
        raise ToolError(f"No stored run of {page_path} with result_date {result_date!r} "
                        f"({result.get('error') or 'FitNesse did not answer with a test result'}). "
                        "List the runs with fitnesse_get_page_history(page_path).")
    summary = _history_summary(result["body"]) or {}
    if root.tag == "suiteResults":
        return {"kind": "suite", "pages": {p["page"]: p for p in summary["pages"]},
                "head": {**({"result_date": result_date} if result_date != "latest" else {}), "date": summary.get("date"),
                         "final_counts": summary.get("final_counts")}}
    res = root.find("result")
    content = (res.findtext("content") if res is not None else None) or ""
    return {"kind": "test", "tables": [b for b in _table_blocks(content) if b[0] is not None],
            "head": {**({"result_date": result_date} if result_date != "latest" else {}), "date": summary.get("date"),
                     "outcome": summary.get("outcome"), "counts": summary.get("counts")}}


def _capped_lines(lines: list[str], limit: int = 40) -> str:
    if len(lines) > limit:
        lines = [*lines[:limit], f"… [{len(lines) - limit} more lines]"]
    return "\n".join(lines)


def _rows_only(block: str) -> str:
    """A table's rows without its number, links and images: those differ in every run."""
    return "\n".join(line for line in block.splitlines() if line.startswith("| "))


def _compare_runs(page_path: str, result_date: str, compare_to: str, timeout_seconds: float) -> dict:
    """What differs between two stored runs of a page. FitNesse's own compareHistory answers with
    the two full reports side by side as HTML, close to a megabyte for one test page."""
    run = _stored_run(page_path, result_date, timeout_seconds)
    other = _stored_run(page_path, compare_to, timeout_seconds)
    if run["kind"] != other["kind"]:
        raise ToolError(f"{result_date!r} is a {run['kind']} run and {compare_to!r} a {other['kind']} run of "
                        f"{page_path}: they cannot be compared.")
    result: dict = {"ok": True, "page": page_path, "run": run["head"], "compared_to": other["head"]}
    differences: list[dict] = []
    if run["kind"] == "suite":
        same = 0
        for page in dict.fromkeys([*run["pages"], *other["pages"]]):
            a, b = run["pages"].get(page), other["pages"].get(page)
            if a and b and a["outcome"] == b["outcome"]:
                same += 1
                continue
            differences.append({
                "page": page,
                "run": a["outcome"] if a else "not in this run",
                "compared_to": b["outcome"] if b else "not in this run",
                **({"run_result_date": a["result_date"]} if a else {}),
                **({"compared_to_result_date": b["result_date"]} if b else {}),
            })
        result["pages_same"] = same
    else:
        ours, theirs = run["tables"], other["tables"]
        matcher = difflib.SequenceMatcher(
            None, [_rows_only(t) for _, t in ours], [_rows_only(t) for _, t in theirs], autojunk=False)
        same = 0
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                same += i2 - i1
                continue
            for k in range(max(i2 - i1, j2 - j1)):
                a = ours[i1 + k] if i1 + k < i2 else None
                b = theirs[j1 + k] if j1 + k < j2 else None
                if a and b:
                    lines = list(difflib.unified_diff(
                        b[1].splitlines()[1:], a[1].splitlines()[1:], lineterm="", n=1))[2:]
                    entry = {"table": a[0], "diff": _capped_lines(lines)}
                else:
                    only = a or b
                    entry = {"table": only[0], "only_in": "run" if a else "compared_to",
                             "text": _capped_lines(only[1].splitlines())}
                differences.append(entry)
        result["tables_same"] = same
    if len(differences) > _MAX_RUN_DIFFERENCES:
        result["differences_left_out"] = len(differences) - _MAX_RUN_DIFFERENCES
    result["differences"] = differences[:_MAX_RUN_DIFFERENCES]
    return result


_RESULT_DATE = re.compile(r"resultDate=(\d+)")


def _int_counts(element) -> dict[str, int]:
    counts = element.find("counts") if element is not None else None
    if counts is None:
        return {}
    return {c.tag: int(c.text or 0) for c in counts if (c.text or "").strip().isdigit()}


def _result_status(counts: dict[str, int]) -> str:
    if counts.get("exceptions"):
        return "error"
    if counts.get("wrong"):
        return "fail"
    if counts.get("right"):
        return "pass"
    return "ignore" if counts.get("ignores") else "no checks"


def _result_date(link: str | None) -> str | None:
    found = _RESULT_DATE.search(link or "")
    return found.group(1) if found else None


def _history_summary(xml_bytes: bytes) -> dict | None:
    """Condense pageHistory XML (a page's runs, a suite run or a test run); None if not one of them."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    text = lambda element, tag: (element.findtext(tag) or "").strip() or None  # noqa: E731
    if root.tag == "PageHistory":
        runs = [
            {"page": text(page, "Name"), "date": text(run, "Date"),
             "result_date": _result_date(text(run, "ResultLink")),
             "pass": int(text(run, "Pass") or 0), "fail": int(text(run, "Fail") or 0)}
            for page in root.iter("Page") for run in page.iter("TestResult")
        ]
        return {"runs": runs}
    log = root.find("executionLog")
    extras = {}
    if log is not None:
        if text(log, "exitCode") not in (None, "0"):
            extras["exit_code"] = int(text(log, "exitCode"))
        for tag, key in (("command", "command"), ("stdOut", "stdout"), ("stdErr", "stderr")):
            if text(log, tag):  # only present when include_* asked for it
                extras[key] = _mask_secrets(text(log, tag))
    if root.tag == "suiteResults":
        pages = []
        for ref in root.iter("pageHistoryReference"):
            counts = _int_counts(ref)
            pages.append({"page": text(ref, "name"), "outcome": _result_status(counts), "counts": counts,
                          "result_date": _result_date(text(ref, "pageHistoryLink")),
                          "run_time_ms": int(text(ref, "runTimeInMillis") or 0)})
        final = root.find("finalCounts")
        return {
            "page": text(root, "rootPath"), "date": text(root, "date"),
            "final_counts": {c.tag: int(c.text or 0) for c in final} if final is not None else {},
            "failed_pages": [p["page"] for p in pages if p["outcome"] in ("fail", "error")],
            "pages": pages, **extras,
        }
    if root.tag == "testResults":
        results = []
        for res in root.iter("result"):
            counts = _int_counts(res)
            content = res.findtext("content") or ""
            document = _parse_document(content)
            entry = {"page": res.findtext("relativePageName") or text(root, "rootPath"),
                     "outcome": _result_status(counts), "counts": counts, "date": text(res, "date"),
                     "run_time_ms": int(text(res, "runTimeInMillis") or 0),
                     "tables": _tables_text(content, document, only_failures=True)}
            passed = sum(1 for t in document["tables"] if not _failed(t))
            if passed:
                entry["tables_without_failures"] = passed
            results.append(entry)
        if len(results) == 1:
            return {**results[0], "page": text(root, "rootPath") or results[0]["page"], **extras}
        return {"page": text(root, "rootPath"), "results": results, **extras}
    return None


# Values of secrets in the test process's output (KDTA's REST logging writes 'Authorization=Bearer
# …', 'X-Api-Key=…', JSON bodies and 'key:value' query parameters), masked before a model sees
# them. The names stay, and an auth scheme (Bearer, Basic): the log still shows what was set.
_SECRET_NAME = r"(?!access-control-)[\w.-]*(?:authorization|api[-_]?key|token|secret|passw(?:or)?d|pwd|j?session[-_]?id|credential)[\w.-]*"
_SECRET_VARIABLE = re.compile(_SECRET_NAME, re.I)
_QUOTE = r'(?:"|&quot;)'
_SCHEME = r"((?:bearer|basic|digest|negotiate|ntlm|token)\s+)?"
_SECRET_QUOTED = re.compile(  # "name": "value with spaces"
    rf"(?i)({_QUOTE}{_SECRET_NAME}{_QUOTE}\s*:\s*{_QUOTE}){_SCHEME}((?:(?!{_QUOTE}).)+)")
_SECRET_PLAIN = re.compile(  # name=value, name: value, ?name=value&
    rf"(?i)(?<![\w.-])({_SECRET_NAME}{_QUOTE}?\s*[=:]\s*{_QUOTE}?){_SCHEME}([^\"&\s,;<]+)")
_COOKIE = re.compile(rf"(?i)(?<![\w.-])([\w.-]*cookie{_QUOTE}?\s*[=:]\s*{_QUOTE}?)((?:(?!{_QUOTE})[^\n<])+)")


def _mask_secrets(text: str) -> str:
    """text with the values of secrets (see above) replaced by ***."""
    def mask(m: re.Match) -> str:
        return f"{m[1]}{m[2] or ''}***"
    text = _COOKIE.sub(lambda m: f"{m[1]}***", text)
    return _SECRET_PLAIN.sub(mask, _SECRET_QUOTED.sub(mask, text))


def _strip_execution_log(raw: bytes, tags: set[str]) -> bytes:
    """Remove the given child elements of every executionLog; non-XML is returned as is."""
    if not tags:
        return raw
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return raw
    for execution_log in root.iter("executionLog"):
        for child in [c for c in execution_log if c.tag in tags]:
            execution_log.remove(child)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


@mcp.tool(
    name="fitnesse_get_properties",
    description=(
        'Returns the properties of a page as JSON (body): its type (Test, Suite; neither = '
        'static), the flags for the actions shown (Edit, Versions, Properties, Refactor, '
        'WhereUsed, RecentChanges, Files, Search), the security flags (secure-read, '
        'secure-write, secure-test), Prune (skipped in suite runs), and Help and Suites (tags) '
        'when set. Change them with fitnesse_save_properties.'
    ),
    tags={"fitnesse", "rest", "pages", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_properties(
    page_path: WikiPath,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Get page properties via responder=properties."""
    return _request(
        "GET",
        page_path,
        params={"responder": "properties", "format": "json"},
        timeout_seconds=timeout_seconds,
    )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_publish",
        description="Publishes the site as static HTML files using destination as the output root path.",
        tags={"fitnesse", "rest", "pages", "write"},
        annotations={"destructiveHint": False, "idempotentHint": True},
    )
    def fitnesse_publish(
        page_path: WikiPath,
        destination: str,
        timeout_seconds: float = _make_timeout(120.0),
    ) -> dict:
        """Publish static HTML via responder=publish."""
        return _request(
            "GET",
            page_path,
            params={"responder": "publish", "destination": destination},
            timeout_seconds=timeout_seconds,
        )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_purge_history",
        description=(
            'Deletes the test history older than `days` days of a page and its children; '
            'purge_global=true that of all pages. Cannot be undone.'
        ),
        tags={"fitnesse", "rest", "pages", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_purge_history(
        days: int,
        page_path: WikiPath = "",
        purge_global: bool = False,
        timeout_seconds: float = _make_timeout(120.0),
    ) -> dict:
        """Purge old history via responder=purgeHistory."""
        params: dict[str, str | None] = {"responder": "purgeHistory", "days": str(days)}
        if purge_global:
            params["purgeGlobal"] = "true"
        return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_raw",
        description="Returns the raw wiki text of the selected page using the raw responder.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_raw(
        page_path: WikiPath,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get raw wiki text via responder=raw."""
        return _request(
            "GET",
            page_path,
            params={"responder": "raw"},
            timeout_seconds=timeout_seconds,
        )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_refactor_screen",
        description="Displays the refactoring screen for the selected page.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_refactor_screen(
        page_path: WikiPath,
        refactor_type: Annotated[
            Literal["replace", "delete", "rename", "move"] | None,
            Field(description="Show only this refactoring form; all forms when omitted."),
        ] = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get refactor screen via responder=refactor."""
        params: dict[str, str | None] = {"responder": "refactor"}
        if refactor_type:
            params["type"] = refactor_type
        return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_rename_file",
        description=(
            'Renames a file or directory of the files section within its directory.'
        ),
        tags={"fitnesse", "rest", "files", "write"},
        annotations={"destructiveHint": False, "idempotentHint": True},
    )
    def fitnesse_rename_file(
        files_path: FilesPath,
        filename: str,
        new_name: str,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Rename a file via responder=renameFile."""
        return _request(
            "GET",
            _files_resource(files_path),
            params={
                "responder": "renameFile",
                "filename": _files_name(filename, "filename"),
                "newName": _files_name(new_name, "new_name"),
            },
            timeout_seconds=timeout_seconds,
        )


@mcp.tool(
    name="fitnesse_rename_page",
    description=(
        'Gives the page a new name below the same parent; its child pages stay with it. To '
        'move it elsewhere, use fitnesse_move_page.'
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_rename_page(
    page_path: WikiPath,
    new_name: PageName,
    refactor_references: Annotated[
        bool, Field(description="Also update references to the renamed page.")
    ] = False,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Rename a page using responder=renamePage."""
    params: dict[str, str | None] = {"responder": "renamePage", "newName": new_name}
    if refactor_references:
        params["refactorReferences"] = None
    result = _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    return _page_change(result, f"Renamed {page_path} to {new_name}.")


@mcp.tool(
    name="fitnesse_rollback_version",
    description=(
        "Makes a saved version the page's current content again. version is an id from "
        'fitnesse_page_versions; look at what changes first with '
        "fitnesse_page_versions(page_path, version, compare_to='latest')."
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": True},
)
def fitnesse_rollback_version(
    page_path: WikiPath,
    version: _VERSION,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Rollback to a saved version via responder=rollback."""
    return _request(
        "GET",
        page_path,
        params={"responder": "rollback", "version": version},
        timeout_seconds=timeout_seconds,
    )


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_rss",
        description=(
            'Returns the RSS feed of the recent changes of a page and its children (XML).'
        ),
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_rss(
        page_path: WikiPath,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get RSS feed via responder=rss."""
        return _request(
            "GET",
            page_path,
            params={"responder": "rss"},
            timeout_seconds=timeout_seconds,
        )


type PageAttribute = Literal[
    "Edit",
    "Versions",
    "Properties",
    "Refactor",
    "WhereUsed",
    "RecentChanges",
    "Files",
    "Search",
    "DisableTestHistory",
    "secure-read",
    "secure-write",
    "secure-test",
    "Prune",
]
PAGE_ATTRIBUTES: tuple[str, ...] = get_args(PageAttribute.__value__)


def _current_properties(page_path: str, timeout_seconds: float) -> dict:
    """Fetch properties?format=json; a missing page yields all flags false and no Help/Suites.

    Raises ToolError when they cannot be read, because saveData and saveProperties
    remove whatever is not sent and guessing would wipe help text, tags or flags.
    """
    result = _request(
        "GET",
        page_path,
        params={"responder": "properties", "format": "json"},
        timeout_seconds=timeout_seconds,
    )
    if not result["ok"] or not isinstance(result["body"], dict):
        raise ToolError(
            f"Could not read current properties of {page_path!r} "
            f"({result.get('error') or 'response is not JSON'}); nothing was saved. "
            "Pass help_text and suites explicitly to save without reading them."
        )
    return result["body"]


def _suites_string(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(tag) for tag in value)
    return str(value or "")


# saveData answers with the merge form (templates/mergePage.vm, same marker in
# 20251025 and 20261101) instead of saving when the page changed after editTime.
_MERGE_FORM_MARKER = 'id="merge-old"'
# Long.MAX_VALUE: never older than the last save, whatever the server's clock says.
_OVERWRITE_EDIT_TIME = str(2**63 - 1)


@mcp.tool(
    name="fitnesse_save_page_content",
    description=(
        'Saves the wiki text of a page, replacing all of it. Read it first with '
        "fitnesse_get_page_data and pass that call's edit_time and ticket_id: if someone else "
        'saved the page since then, nothing is saved and the call fails; read the page again, '
        'merge, and save again. Without edit_time the save overwrites whatever is there. The '
        "page's help text and tags are kept unless help_text or suites are given; an empty "
        'string removes them.'
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_save_page_content(
    page_path: WikiPath,
    page_content: PageContent,
    edit_time: Annotated[str | None, Field(description="edit_time returned by fitnesse_get_page_data; enables the conflict check. Omit to overwrite.", pattern=r"^\d+$")] = None,
    ticket_id: Annotated[str | None, Field(description="ticket_id returned by the same fitnesse_get_page_data call; saves with the same ticket never conflict with each other.", pattern=r"^-?\d+$")] = None,
    redirect: Annotated[str | None, Field(description="Not needed: where FitNesse redirects a browser after the save.")] = None,
    help_text: Annotated[str | None, Field(description="New help text; omit to keep the current one, '' to remove it.")] = None,
    suites: Annotated[str | None, Field(description="New comma-separated tags; omit to keep the current ones, '' to remove them.")] = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Save a FitNesse page using responder=saveData form payload."""
    if help_text is None or suites is None:
        current = _current_properties(page_path, timeout_seconds)
        if help_text is None:
            help_text = str(current.get("Help") or "")
        if suites is None:
            suites = _suites_string(current.get("Suites"))

    # Without editTime FitNesse compares against 0, so a page saved through the UI
    # since the server started would silently turn this save into a merge form.
    form_data: dict[str, str] = {
        "pageContent": page_content,
        "editTime": edit_time or _OVERWRITE_EDIT_TIME,
    }
    if ticket_id:
        form_data["ticketId"] = ticket_id
    if redirect:
        form_data["redirect"] = redirect
    if help_text:
        form_data["helpText"] = help_text
    if suites:
        form_data["suites"] = suites

    body = parse.urlencode(form_data)
    result = _request(
        "POST",
        page_path,
        params={"responder": "saveData"},
        body=body,
        content_type="application/x-www-form-urlencoded",
        timeout_seconds=timeout_seconds,
    )
    if result.get("ok") and _MERGE_FORM_MARKER in (result.get("body") or ""):
        raise ToolError(
            f"Not saved: {page_path} was changed by someone else after edit_time {edit_time}. "
            "FitNesse answered with its merge form. Read the page again, merge your changes "
            "into the current content, and save with a fresh edit_time (or without edit_time "
            "to overwrite)."
        )
    return _page_change(result, f"Saved {page_path}.")


@mcp.tool(
    name="fitnesse_save_properties",
    description=(
        "Saves page properties via POST: pageType, attribute flags, Suites tags and HelpText. "
        "FitNesse resets every attribute that is not sent, so each argument left out keeps the "
        "page's current value; `attributes` is the complete list of flags that end up set. "
        "Pass an empty string or list to clear a value."
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_save_properties(
    page_path: WikiPath,
    page_type: PageType | None = None,
    suites: str | None = None,
    help_text: str | None = None,
    attributes: list[PageAttribute] | None = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Save properties with responder=saveProperties, merged onto the current ones."""
    if page_type is None or suites is None or help_text is None or attributes is None:
        current = _current_properties(page_path, timeout_seconds)
        if page_type is None:
            page_type = (
                "Test" if current.get("Test") else "Suite" if current.get("Suite") else "Static"
            )
        if suites is None:
            suites = _suites_string(current.get("Suites"))
        if help_text is None:
            help_text = str(current.get("Help") or "")
        if attributes is None:
            attributes = [a for a in PAGE_ATTRIBUTES if current.get(a) is True]

    # "PageType": the only name FitNesse 20251025 reads; 20261101 still accepts it.
    form_data: dict[str, str] = {"PageType": page_type}
    if suites:
        form_data["Suites"] = suites
    if help_text:
        form_data["HelpText"] = help_text
    for attr in dict.fromkeys(attributes):
        form_data[attr] = "on"

    body = parse.urlencode(form_data)
    return _request(
        "POST",
        page_path,
        params={"responder": "saveProperties"},
        body=body,
        content_type="application/x-www-form-urlencoded",
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(
    name="fitnesse_search",
    description=(
        "Finds pages by name (search_type='title') or by content (search_type='content'). "
        "Result: matches (page, type Test/Suite, tags, last_modified) and count. Both are plain "
        "text matches, not regular expressions; a title match is any name containing the "
        "search_string. method_search=true (content only) finds the pages that use a method or "
        "scenario: give a wiki line that evaluates to it, e.g. '|click|x|'. Searches page_path and "
        "its children; search_scope names an ancestor page instead, or 'root' for the whole wiki "
        "(a content search over the whole wiki can take minutes)."
    ),
    tags={"fitnesse", "rest", "search", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_search(
    search_string: Annotated[str, Field(description="Text to find, or with method_search a wiki line such as '|click|x|'.")],
    page_path: WikiPath = "",
    search_type: Literal["title", "content"] = "content",
    method_search: Annotated[bool, Field(description="Find the pages that use a method or scenario (content only).")] = False,
    search_scope: Annotated[
        str | None,
        Field(description="Ancestor page to search below, or 'root' for the whole wiki. Default: page_path."),
    ] = None,
    timeout_seconds: float = _make_timeout(120.0),
) -> dict:
    """Search pages with responder=search, results parsed from its HTML."""
    if method_search and search_type != "content":
        raise ToolError("method_search only works with search_type='content'")
    # without isTextSearch (or isMethodSearch) netcare's FitNesse answers with the search form only
    params: dict[str, str | None] = {
        "responder": "search",
        "searchString": search_string,
        "searchType": search_type,
        "isMethodSearch" if method_search else "isTextSearch": None,
    }
    if search_scope:
        params["searchScope"] = search_scope
    result = _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    return _search_matches(result)


_SEARCH_TABLE = re.compile(r'<table id="searchResultsTable".*?<tbody>(.*?)</tbody>', re.S)
_SEARCH_ROW = re.compile(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td>(.*?)</td>", re.S)
_SEARCH_FEEDBACK = re.compile(r"getElementById\(\"feedback\"\)\.innerHTML = '([^']*)'")
_MAX_MATCHES = 200


def _search_matches(result: dict) -> dict:
    """The rows of a search responder's result table as matches; the answer as it is if there is none."""
    body = result.get("body")
    if not result.get("ok") or result.get("truncated") or not isinstance(body, str):
        return result
    table = _SEARCH_TABLE.search(body)
    if not table:
        return result
    matches = []
    for kind, page, tags, modified in _SEARCH_ROW.findall(table.group(1)):
        match = {"page": _plain_text(page), "type": _plain_text(kind) or None,
                 "tags": _plain_text(tags) or None, "last_modified": _plain_text(modified) or None}
        matches.append({k: v for k, v in match.items() if v})
    feedback = _SEARCH_FEEDBACK.search(body)
    del result["body"]
    result["count"] = len(matches)
    result["matches"] = matches[:_MAX_MATCHES]
    if len(matches) > _MAX_MATCHES:
        result["left_out"] = len(matches) - _MAX_MATCHES
    if not matches and feedback:
        result["note"] = feedback.group(1)
    return result


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_search_form",
        description="Returns the search form used to configure page searches.",
        tags={"fitnesse", "rest", "search", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_search_form(
        page_path: WikiPath = "",
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get search form via responder=searchForm."""
        return _request(
            "GET",
            page_path,
            params={"responder": "searchForm"},
            timeout_seconds=timeout_seconds,
        )


if _env_flag("FITNESSE_ALLOW_SHUTDOWN"):

    @mcp.tool(
        name="fitnesse_shutdown",
        description="Shuts the FitNesse server down through the shutdown responder.",
        tags={"fitnesse", "rest", "admin", "control"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_shutdown(
        timeout_seconds: float = _make_timeout(10.0),
    ) -> dict:
        """Shutdown FitNesse via responder=shutdown."""
        return _request(
            "GET", "", params={"responder": "shutdown"}, timeout_seconds=timeout_seconds
        )


@mcp.tool(
    name="fitnesse_stop_test",
    description=(
        'Stops running test and suite runs of this FitNesse, whoever started them: all of '
        'them, or the one with test_process_id.'
    ),
    tags={"fitnesse", "rest", "tests", "admin", "control"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_stop_test(
    test_process_id: Annotated[str | None, Field(description="Id of one run, as in the stop link FitNesse shows while it runs. Omit to stop all.")] = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Stop tests with responder=stoptest for one process or all tests."""
    params: dict[str, str | None] = {"responder": "stoptest"}
    if test_process_id:
        params["id"] = test_process_id
    return _request("GET", "", params=params, timeout_seconds=timeout_seconds)


def _suite_filter_params(
    suite_filter: str | None,
    run_tests_matching_any_tag: str | None,
    run_tests_matching_all_tags: str | None,
    exclude_suite_filter: str | None,
    first_test: str | None,
) -> dict[str, str | None]:
    """Test selection inputs shared by suite, partition and overview."""
    params: dict[str, str | None] = {}
    for key, value in (
        ("suiteFilter", suite_filter),
        ("runTestsMatchingAnyTag", run_tests_matching_any_tag),
        ("runTestsMatchingAllTags", run_tests_matching_all_tags),
        ("excludeSuiteFilter", exclude_suite_filter),
        ("firstTest", first_test),
    ):
        if value:
            params[key] = value
    return params


@mcp.tool(
    name="fitnesse_run_suite",
    description=(
        "Runs the test pages below a suite page and returns FitNesse's report (body). Narrow "
        'the selection with tags (suite_filter, run_tests_matching_all_tags, '
        'exclude_suite_filter), first_test, or a partition (partition_count with '
        'partition_index). The run is stored: fitnesse_get_suite_overview(page_path) then '
        "shows each page's outcome, fitnesse_get_page_history a failing page's tables. Do not "
        "combine nohistory with format='xml': that report is built from the history. A run that "
        "takes longer than the server's run timeout fails here but may go on in FitNesse: "
        "fitnesse_stop_test stops it."
    ),
    tags={"fitnesse", "rest", "tests", "execute"},
    annotations={"destructiveHint": False},
)
def fitnesse_run_suite(
    page_path: WikiPath,
    debug: Annotated[bool, Field(description="Run inside the FitNesse process instead of a test process of its own (Slim only; for Java debugging). Normally leave it off.")] = False,
    suite_filter: _ANY_TAG = None,
    exclude_suite_filter: _NO_TAG = None,
    first_test: _FIRST_TEST = None,
    nohistory: _NO_HISTORY = False,
    includehtml: Annotated[bool, Field(description="format='xml' only: also return each page's result as HTML (large).")] = False,
    nochunk: _NO_CHUNK = False,
    variables: _RUN_VARIABLES = None,
    run_tests_matching_any_tag: Annotated[str | None, Field(description="Same as suite_filter; wins when both are given.")] = None,
    run_tests_matching_all_tags: _ALL_TAGS = None,
    partition_count: Annotated[int | None, Field(description="Split the selected tests into this many parts and run only one of them.", ge=1)] = None,
    partition_index: Annotated[int | None, Field(description="Which part to run, counted from 0.", ge=0)] = None,
    format: RunFormat = "xml",
    timeout_seconds: float | None = _make_run_timeout(),
) -> dict:
    """Run a suite with full documented controls for responder=suite."""
    if (partition_count is None) != (partition_index is None):
        raise ToolError("Give partition_count and partition_index together")
    if partition_count is not None and partition_index >= partition_count:
        raise ToolError("partition_index must be smaller than partition_count")
    params: dict[str, str | None] = {"responder": "suite", "format": format}
    if debug:
        params["debug"] = None
    params.update(
        _suite_filter_params(
            suite_filter,
            run_tests_matching_any_tag,
            run_tests_matching_all_tags,
            exclude_suite_filter,
            first_test,
        )
    )
    if partition_count is not None:
        params["partitionCount"] = str(partition_count)
        params["partitionIndex"] = str(partition_index)
    if nohistory:
        params["nohistory"] = None
    if includehtml:
        params["includehtml"] = None
    if nochunk:
        params["nochunk"] = None
    params = _merge_extra(params, variables)
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_manage_symlink",
        description=(
            'Manages the symbolic links of a page, which show another page as its child. Create: '
            "link_name and link_path (the target page). Remove: removal, the link's name. Rename: "
            "rename, the link's name, and newname."
        ),
        tags={"fitnesse", "rest", "pages", "write"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_manage_symlink(
        page_path: WikiPath,
        removal: str | None = None,
        rename: str | None = None,
        newname: str | None = None,
        link_name: str | None = None,
        link_path: str | None = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Create/update/remove symlinks via responder=symlink."""
        params: dict[str, str | None] = {"responder": "symlink"}
        if removal:
            params["removal"] = removal
        if rename:
            params["rename"] = rename
        if newname:
            params["newname"] = newname
        if link_name:
            params["linkName"] = link_name
        if link_path:
            params["linkPath"] = link_path
        return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


@mcp.tool(
    name="fitnesse_run_test",
    description=(
        "Runs a test page from start to end and returns FitNesse's report (body): format 'xml' "
        'has every table with its results and can be large. Unless nohistory=true the run is '
        "stored: fitnesse_get_page_history(page_path, result_date='latest') then shows it "
        "condensed, with only the failing tables. A run that takes longer than the server's run "
        "timeout fails here but may go on in FitNesse: fitnesse_stop_test stops it."
    )
    + (" To step through a failing test and try out fixes, use fitnesse_debug_start." if "debug" in FITNESSE_TOOLSETS else ""),
    tags={"fitnesse", "rest", "tests", "execute"},
    annotations={"destructiveHint": False},
)
def fitnesse_run_test(
    page_path: WikiPath,
    debug: Annotated[bool, Field(description="Run inside the FitNesse process instead of a test process of its own (Slim only; for Java debugging). Normally leave it off.")] = False,
    nochunk: _NO_CHUNK = False,
    variables: _RUN_VARIABLES = None,
    nohistory: _NO_HISTORY = False,
    format: RunFormat = "xml",
    timeout_seconds: float | None = _make_run_timeout(),
) -> dict:
    """Run a test with responder=test and optional controls."""
    params: dict[str, str | None] = {"responder": "test", "format": format}
    if debug:
        params["debug"] = None
    if nochunk:
        params["nochunk"] = None
    if nohistory:
        params["nohistory"] = None
    params = _merge_extra(params, variables)
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_test_history",
        description="Displays current test history for a page subtree or all history. Returns XML; results and trim only shape the HTML view (format='html').",
        tags={"fitnesse", "rest", "history", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_test_history(
        page_path: WikiPath = "",
        format: Literal["xml", "html"] = "xml",
        results: Annotated[
            int | None, Field(ge=1, description="HTML only: most recent results shown per page.")
        ] = None,
        trim: Annotated[
            str | None, Field(description="HTML only: prefix removed from the page names shown.")
        ] = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get test history via responder=testHistory."""
        if format == "xml" and (results is not None or trim):
            raise ToolError("results and trim only apply to format='html'")
        params: dict[str, str | None] = {"responder": "testHistory"}
        if format == "xml":
            params["format"] = "xml"
        if results is not None:
            params["results"] = str(results)
        if trim:
            params["trim"] = trim
        return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_upload_file",
        description=(
            'Stores a file in a directory of the files section, replacing a file of the same name. '
            'Give the content as text (content) or base64-encoded for binary files '
            '(content_base64).'
        ),
        tags={"fitnesse", "rest", "files", "write"},
        annotations={"destructiveHint": False, "idempotentHint": True},
    )
    def fitnesse_upload_file(
        files_path: FilesPath,
        filename: str,
        content: str | None = None,
        content_base64: str | None = None,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """Upload file content to files section via responder=upload and multipart form data."""
        resource = _files_resource(files_path)
        filename = _files_name(filename, "filename")

        if (content is None) == (content_base64 is None):
            raise ToolError("Provide exactly one of content or content_base64")

        if content is not None:
            file_bytes = content.encode("utf-8")
        else:
            try:
                file_bytes = b64decode(content_base64, validate=True)
            except binascii.Error as exc:
                raise ToolError(f"content_base64 is not valid base64: {exc}") from exc

        if len(file_bytes) > MAX_TRANSFER_BYTES:
            logger.warning(
                "Upload of %s rejected: %d bytes exceeds FITNESSE_FILES_MAX_TRANSFER_BYTES=%d",
                filename,
                len(file_bytes),
                MAX_TRANSFER_BYTES,
            )
            return {
                "ok": False,
                "error": f"File is {len(file_bytes)} bytes, limit is {MAX_TRANSFER_BYTES}",
            }

        body, content_type = _build_multipart_form_data(
            fields={}, file_field="file", filename=filename, file_bytes=file_bytes
        )

        return _request(
            "POST",
            resource,
            params={"responder": "upload"},
            body=body,
            content_type=content_type,
            timeout_seconds=timeout_seconds,
        )


@mcp.tool(
    name="fitnesse_page_versions",
    description=(
        "The saved versions of a page's wiki text. Without version: current (who saved the page "
        "last, and when) and versions (version, author, age), newest first; these are earlier states, the "
        "newest need not equal the current page. With version: that "
        "version as rendered text (content). With version and compare_to (another version, or "
        "'latest' for the current page): diff, a unified diff of the wiki text (- lines are from version, + lines from "
        "compare_to). A version id ends with the time it was saved (yyyyMMddHHmmss). "
        "fitnesse_rollback_version makes a version the current one again."
    ),
    tags={"fitnesse", "rest", "history", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_page_versions(
    page_path: WikiPath,
    version: Annotated[str | None, Field(description="A version id from the list, e.g. 'jane-20261109143000'.")] = None,
    compare_to: Annotated[str | None, Field(description="Another version id, or 'latest'. Needs version.")] = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """responder=versions, viewVersion or compareVersions, condensed (all three answer HTML only)."""
    if compare_to and not version:
        raise ToolError("compare_to needs version: pass the version to compare, and compare_to='latest' "
                        "or another version id.")
    if not version:
        params: list[tuple[str, str | None]] = [("responder", "versions")]
    elif not compare_to:
        params = [("responder", "viewVersion"), ("version", version)]
    else:
        params = [("responder", "compareVersions"), (f"Version_{version}", None)]
        if compare_to != "latest":
            params.append((f"Version_{compare_to}", None))
    result = _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    body = result.get("body")
    if not result.get("ok") or result.get("truncated") or not isinstance(body, str):
        return result
    if not version:
        versions = [{"version": _unescape_html(v), "author": _plain_text(author), "age": _plain_text(age)}
                    for v, author, age in _VERSION_ROW.findall(body)]
        current = _VERSION_CURRENT.search(body)
        if not versions and not current:
            return result  # not the versions view
        condensed = {"current": _plain_text(current.group(1)) if current else None, "versions": versions}
    elif not compare_to:
        if "<article" not in body:
            return result
        article = body[body.index("<article"):]
        for end in ('<div id="-shortcut-keys"', "</article>"):  # the help dialog sits inside the article
            if end in article:
                article = article[:article.index(end)]
                break
        condensed = {"version": version, "content": _plain_text(_HIDDEN_HTML.sub("", article), layout=True)}
    else:
        lines = [_unescape_html(line) for line in _DIFF_LINE.findall(body)]
        if not lines:
            if "diff_" not in body and "Comparing" not in body:
                return result
            return {**{k: v for k, v in result.items() if k != "body"}, "version": version,
                    "compare_to": compare_to, "diff": "", "note": "The two are the same."}
        condensed = {"version": version, "compare_to": compare_to, "diff": "\n".join(lines)}
    del result["body"]
    result.update(condensed)
    return result


_VERSION_ROW = re.compile(
    r'name="Version_([^"]+)"\s*/?>\s*</td>\s*<td>.*?</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>', re.S)
_VERSION_CURRENT = re.compile(r"<p>\s*Current:\s*(.*?)</p>", re.S)
_DIFF_LINE = re.compile(r'<td class="diff_line[^"]*">(.*?)</td>', re.S)
_BLOCK_END = re.compile(r"(?i)<br\s*/?>|</(?:tr|p|div|li|h[1-6]|center|pre|table)\s*>")
_CELL_END = re.compile(r"(?i)</t[dh]\s*>")


def _plain_text(html: str, layout: bool = False) -> str:
    """HTML as text. layout: one line per block or table row, cells separated by ' | '."""
    if layout:
        html = _BLOCK_END.sub("\n", _CELL_END.sub(" | ", html))
    text = _unescape_html(re.sub(r"<[^>]+>", "" if layout else " ", html))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line) if layout else " ".join(lines).strip()


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_where_used",
        description="Returns pages that contain links or references to the selected page.",
        tags={"fitnesse", "rest", "search", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_where_used(
        page_path: WikiPath,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Find references to a page via responder=whereUsed."""
        return _request(
            "GET",
            page_path,
            params={"responder": "whereUsed"},
            timeout_seconds=timeout_seconds,
        )


@mcp.tool(
    name="fitnesse_get_variables",
    description=(
        "The variables a page can use as ${NAME}, its own and those inherited from parent pages: "
        "variables (name, value, location = the page that defines it; none for FitNesse's own). "
        "Values of passwords, tokens and the like are masked as ***."
    ),
    tags={"fitnesse", "rest", "pages", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_variables(
    page_path: WikiPath,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Variables of a page via responder=variables (HTML only), condensed."""
    result = _request(
        "GET",
        page_path,
        params={"responder": "variables"},
        timeout_seconds=timeout_seconds,
    )
    body = result.get("body")
    if not result.get("ok") or result.get("truncated") or not isinstance(body, str):
        return result
    table = re.search(r'<table id="definedVariables".*?<tbody>(.*?)</tbody>', body, re.S)
    if not table:
        return result
    variables = []
    for name, value, location in _VARIABLE_ROW.findall(table.group(1)):
        name = _plain_text(name)
        entry = {"name": name, "value": "***" if _SECRET_VARIABLE.fullmatch(name) else _plain_text(value)}
        if _plain_text(location):
            entry["location"] = _plain_text(location)
        variables.append(entry)
    del result["body"]
    result["variables"] = variables
    return result


_VARIABLE_ROW = re.compile(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>", re.S)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_page_content",
        description="Convenience helper that reads edit responder output for page content retrieval workflows.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_page_content(
        page_path: WikiPath,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Convenience helper for responder=edit response retrieval."""
        return _request(
            "GET",
            page_path,
            params={"responder": "edit", "format": "json"},
            timeout_seconds=timeout_seconds,
        )


# ---------------------------------------------------------------------------
# Responders documented in RestfulServices (FitNesse 20251025 and 20261101)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fitnesse_replace",
    description=(
        "Replaces text in the selected page and all its children and saves every changed page; "
        "returns an HTML table of the changed pages. Text replace: search_string is a regular "
        "expression. method_replace=true: search_string is a wiki line that evaluates to a "
        "method or scenario (e.g. '|click|x|'). search_scope limits it to an ancestor page or "
        "'root'. Try the search with fitnesse_search first: this cannot be undone in one step."
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": True},
)
def fitnesse_replace(
    page_path: WikiPath,
    search_string: str,
    replacement_string: str,
    method_replace: bool = False,
    search_scope: Annotated[
        str | None,
        Field(description="Ancestor page to work below, or 'root' for the whole wiki."),
    ] = None,
    timeout_seconds: float = _make_timeout(120.0),
) -> dict:
    """Search and replace via responder=replace."""
    params: dict[str, str | None] = {
        "responder": "replace",
        "searchString": search_string,
        "replacementString": replacement_string,
    }
    if method_replace:
        params["isMethodReplace"] = None
    if search_scope:
        params["searchScope"] = search_scope
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


@mcp.tool(
    name="fitnesse_get_suite_overview",
    description=(
        "The latest result of each test page of a suite, without running anything: summary "
        "(passed, not_run, failed), pages (page, outcome, result_date) and failed_pages. For why a "
        "page failed, call fitnesse_get_page_history(page, result_date). Takes the same test "
        "selection as fitnesse_run_suite. raw=true returns FitNesse's HTML instead.\n"
        "These are the stored results: after a run of your own, check that result_date is from "
        "that run; an older one means the run did not finish or was not stored."
    ),
    tags={"fitnesse", "rest", "tests", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_suite_overview(
    page_path: WikiPath,
    suite_filter: TagList = None,
    run_tests_matching_any_tag: TagList = None,
    run_tests_matching_all_tags: TagList = None,
    exclude_suite_filter: TagList = None,
    first_test: str | None = None,
    raw: Annotated[bool, Field(description="Return FitNesse's HTML (body) instead of the condensed result.")] = False,
    timeout_seconds: float = _make_timeout(60.0),
) -> dict:
    """Suite tree with latest results via responder=overview (HTML only, no format parameter)."""
    params: dict[str, str | None] = {"responder": "overview"}
    params.update(
        _suite_filter_params(
            suite_filter,
            run_tests_matching_any_tag,
            run_tests_matching_all_tags,
            exclude_suite_filter,
            first_test,
        )
    )
    result = _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    summary = None if raw or not result.get("ok") or result.get("truncated") else _overview_summary(result.get("body"))
    if summary is not None:
        del result["body"]
        result.update(summary)
    return result


_OVERVIEW_COUNT = re.compile(r"<td>\s*(\d+)\s*</td>\s*<td>[^<]*</td>\s*<td>\s*Test\(s\) (Passed|Not Run|Failed)")
_OVERVIEW_PAGE = re.compile(r'<li class="completion_test">\s*<a href="([^"?]+)(?:\?[^"]*)?" class="([^"]*)"')
_OVERVIEW_STATUS = {"done": "pass", "pass": "pass", "fail": "fail", "error": "error", "unrun": "not run"}


def _overview_summary(body) -> dict | None:
    """Counts and per-page status from the overview responder's HTML; None if it isn't one."""
    if not isinstance(body, str) or "suiteOverview" not in body:
        return None
    counts = {k.lower().replace(" ", "_"): int(n) for n, k in _OVERVIEW_COUNT.findall(body)}
    pages = []
    for match in _OVERVIEW_PAGE.finditer(body):
        href = _unescape_html(match.group(0))
        pages.append({"page": match.group(1), "outcome": _OVERVIEW_STATUS.get(match.group(2), match.group(2) or "not run"),
                      "result_date": _result_date(href)})
    if not counts and not pages:
        return None
    return {"summary": counts, "failed_pages": [p["page"] for p in pages if p["outcome"] in ("fail", "error")],
            "pages": pages}


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_execution_log",
        description=(
            "Shows the execution log of a test run of the page: test system, command, exit code, "
            "stdout/stderr and exceptions. result_date is a timestamp (yyyyMMddHHmmss, as in "
            "fitnesse_get_page_history) or 'latest' (default). Returns HTML; values of secrets "
            "(Authorization, API keys, tokens, cookies, passwords) are masked as ***."
        ),
        tags={"fitnesse", "rest", "history", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_execution_log(
        page_path: WikiPath,
        result_date: Annotated[
            str, Field(pattern=r"^(latest|\d{14})$", description="yyyyMMddHHmmss or 'latest'.")
        ] = "latest",
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Execution log via responder=executionLog."""
        result = _request(
            "GET",
            page_path,
            params={"responder": "executionLog", "resultDate": result_date},
            timeout_seconds=timeout_seconds,
        )
        if isinstance(result.get("body"), str):
            result["body"] = _mask_secrets(result["body"])
        return result

    @mcp.tool(
        name="fitnesse_partition_preview",
        description=(
            "Previews how fitnesse_run_suite(partition_count=...) distributes the suite's tests over "
            "partitions, without running them. Takes the same test selection as fitnesse_run_suite. "
            "Returns tab-separated lines (format='tsv', default) or HTML."
        ),
        tags={"fitnesse", "rest", "tests", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_partition_preview(
        page_path: WikiPath,
        partition_count: Annotated[int, Field(ge=1)],
        format: Literal["tsv", "html"] = "tsv",
        suite_filter: TagList = None,
        run_tests_matching_any_tag: TagList = None,
        run_tests_matching_all_tags: TagList = None,
        exclude_suite_filter: TagList = None,
        first_test: str | None = None,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """Partition preview via responder=partition."""
        params: dict[str, str | None] = {
            "responder": "partition",
            "partitionCount": str(partition_count),
        }
        if format == "tsv":
            params["format"] = "tsv"
        params.update(
            _suite_filter_params(
                suite_filter,
                run_tests_matching_any_tag,
                run_tests_matching_all_tags,
                exclude_suite_filter,
                first_test,
            )
        )
        return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)

    @mcp.tool(
        name="fitnesse_get_proxy_data",
        description=(
            "Returns the serialized page data the import responder uses: type 'pages' (child "
            "pages), 'data', 'versions', or 'meat' (with version: that version's data)."
        ),
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_proxy_data(
        page_path: WikiPath,
        type: Literal["pages", "data", "versions", "meat"],
        version: str | None = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Serialized page data via responder=proxy."""
        params: dict[str, str | None] = {"responder": "proxy", "type": type}
        if version:
            params["version"] = version
        return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)

if _env_flag("FITNESSE_ALLOW_ACCOUNT_ADMIN"):

    @mcp.tool(
        name="fitnesse_save_account",
        description=(
            "Changes FitNesse accounts (responder=saveAccount), as the account this server logs "
            "in with. action='change_password': new password for that account "
            "(current_password, new_password). 'create_user' and 'delete_user' need the admin "
            "user: user_name, plus user_password for create_user. Only on explicit request."
        ),
        tags={"fitnesse", "rest", "admin", "control"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_save_account(
        action: Literal["change_password", "create_user", "delete_user"],
        current_password: str | None = None,
        new_password: str | None = None,
        user_name: str | None = None,
        user_password: str | None = None,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Account changes via responder=saveAccount (POST form)."""
        if action == "change_password":
            if not (current_password and new_password):
                raise ToolError("change_password needs current_password and new_password")
            form = {
                "changePassword": "yes",
                "CurrentPasswordText": current_password,
                "NewPasswordText": new_password,
                "ConfirmPasswordText": new_password,
            }
        elif action == "create_user":
            if not (user_name and user_password):
                raise ToolError("create_user needs user_name and user_password")
            form = {"createUser": "yes", "UserNameText": user_name, "UserPasswordText": user_password}
        else:
            if not user_name:
                raise ToolError("delete_user needs user_name")
            form = {"deleteUser": "yes", "UserNameText": user_name}
        return _request(
            "POST",
            "",
            params={"responder": "saveAccount"},
            body=parse.urlencode(form),
            content_type="application/x-www-form-urlencoded",
            timeout_seconds=timeout_seconds,
        )


# ---------------------------------------------------------------------------
# KDTA debug mode: responder=debugTest and responder=debugTable.
# These responders exist only in netcare's FitNesse fork (fitnesse-netcare-mod),
# so the tools are registered only with debug in FITNESSE_TOOLSETS.
# ---------------------------------------------------------------------------

DEBUG_TOOLSET = "debug" in FITNESSE_TOOLSETS
# Taking over a debug session this server did not start (maybe a person's in the browser).
# Off by default: the MCP server is meant to have its own FitNesse account and its own sessions.
ALLOW_ADOPT = _env_flag("FITNESSE_ALLOW_ADOPT")
# Must exceed the server's debugTest.timeoutMinutes (default 10 min), which answers 504 itself.
_debug_timeout_env = float(os.getenv("FITNESSE_DEBUG_TIMEOUT_SECONDS", "660"))
DEBUG_TIMEOUT_SECONDS = _debug_timeout_env if _debug_timeout_env > 0 else None
# Raw debug responses are condensed before they reach the model, so allow more than MAX_BYTES.
DEBUG_MAX_BYTES = int(os.getenv("FITNESSE_DEBUG_MAX_RESPONSE_BYTES", str(16 * 1024 * 1024)))
DEBUG_MAX_CELL_CHARS = int(os.getenv("FITNESSE_DEBUG_MAX_CELL_CHARS", "1500"))
_DEBUG_COOKIE = "debugTestSession"

type DebugPath = Annotated[
    str | None,
    Field(
        description="Dotted wiki page path. Omit to use the current page of the debug session."
    ),
]


@dataclass
class _DebugState:
    """What this MCP server knows about its debug session on one FitNesse account."""

    session: str | None = None
    page: str | None = None  # the page start was called on (classpath, rights check)
    current_page: str | None = None  # page whose storytest is running
    suite: bool = False
    log_next: int = 0
    cookie: str | None = None  # only used without Basic Auth
    page_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    setup_ran: str | None = None  # page whose SetUp run_page(setup_only=true) ran in the current test
    setup_seen: str | None = None  # page whose SetUp ran in the current test, by setup_only or a full run
    # Single-page sessions: FitNesse runs SuiteSetUp/SuiteTearDown as pages of their own, also on
    # ?test of one page; pageHtml leaves them out. From suitePages of the page itself.
    suite_setup: str | None = None
    suite_teardown: str | None = None
    frame_started: bool = False
    suite_setup_pending: bool = False  # the SuiteSetUp stopped at a failure; the page has not begun
    # Whole-page runs of current_page stop after a failing table: where the next one goes on
    # (index as in fitnesse_debug_page_tables) and how many tables the page had then.
    next_table: int = 0
    table_count: int | None = None
    # Tables of current_page that ran in its test (indices as above), and its TearDown tables as
    # its last pageHtml showed them: nextPage and end run no TearDown, so _end_test sends the rest.
    ran_tables: set[int] = field(default_factory=set)
    teardown_tables: list[int] = field(default_factory=list)
    # run_wiki calls whose tables all passed, for saving as the test (fitnesse_debug_steps). Kept
    # by reset (end, a lost session), so they can still be saved; a new start clears them.
    steps: list[dict] = field(default_factory=list)
    step_count: int = 0
    # Pages fitnesse_debug_start listed (suitePages): run_page takes them without a type check.
    pages: set[str] = field(default_factory=set)
    # When the last runTable began (this server's clock, epoch ms): the log lines of a failing
    # table are those written from then on (_stop_log).
    table_started: float | None = None
    # The session's defined actions as last read: a busy session can't answer definedActions.
    defined_actions: list[dict] | None = None
    # A call that changes the session timed out (or got 504): the process may still be running it,
    # and it answers definedAction(s) only after that (_session_busy asks status).
    maybe_busy: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        self.session = self.page = self.current_page = self.setup_ran = self.setup_seen = None
        self.suite_setup = self.suite_teardown = None
        self.frame_started = self.suite_setup_pending = False
        self.next_table, self.table_count = 0, None
        self.ran_tables, self.teardown_tables = set(), []
        self.suite = False
        self.log_next = 0
        self.page_counts = {}
        self.pages = set()
        self.table_started, self.defined_actions, self.maybe_busy = None, None, False


def _page_key(path: str | None) -> str | None:
    """A page path as this server compares it: FitNesse answers some with a leading dot."""
    return (path or "").strip().lstrip(".") or None


_DEBUG_STATES: dict[tuple[str, str], _DebugState] = {}
_DEBUG_STATES_LOCK = threading.Lock()


def _debug_state() -> _DebugState:
    key = (
        _base_url().rstrip("/"),
        _credentials()[0] or "",
    )
    with _DEBUG_STATES_LOCK:
        return _DEBUG_STATES.setdefault(key, _DebugState())


def _without_secrets(value):
    """Drop every *password* key (e.g. liveViewPassword, the grid's VNC password)."""
    if isinstance(value, dict):
        return {
            k: _without_secrets(v)
            for k, v in value.items()
            if "password" not in str(k).lower()
        }
    if isinstance(value, list):
        return [_without_secrets(v) for v in value]
    return value


_DEBUG_HINTS = {
    403: "No permission for this page.",
    404: "The page does not exist.",
    423: "The test process is still working on a document that timed out. Poll fitnesse_debug_status until busy is false, or call fitnesse_debug_end with abort=true.",
    429: "Too many debug sessions on this FitNesse (debugTest.maxSessions). Try again later.",  # forks before mod #159
    500: "If the session crashed, read the log via fitnesse_debug_status and start a new session.",
    504: "The document did not finish within debugTest.timeoutMinutes; the process keeps working on it. Poll fitnesse_debug_status (shows the log) until busy is false, or call fitnesse_debug_end with abort=true.",
}


_DEBUG_CODE_HINTS = {
    "notLoaded": "The call runs no defined action in this session: check its keywords and number of "
                 "arguments with fitnesse_debug_inspect(kind='definedActions'), or load its page with "
                 "|define actions at|.",
    "unknownPlace": "The definition is not on a page (a |define action| table in the test, or |define "
                    "actions at| with |from| another root), or its page no longer has it: edit it where it "
                    "is (fitnesse_table_source of its page).",
    "sessionRequired": "Several debug sessions of this account are running and this server knows none of "
                       "them: fitnesse_debug_status lists them in 'sessions'; "
                       + ("continue one with fitnesse_debug_status(adopt=true, session=...) or " if ALLOW_ADOPT else "")
                       + "start one with fitnesse_debug_start.",
    "tooManySessions": "Too many debug sessions, for this FitNesse account (debugTest.maxSessionsPerUser) or "
                       "on the whole FitNesse (debugTest.maxSessions). fitnesse_debug_status lists the "
                       "account's running sessions in 'sessions'; "
                       + ("take over one that is no longer needed with fitnesse_debug_status(adopt=true, "
                          "session=...) and end it, or wait: " if ALLOW_ADOPT else
                          "this server leaves those it did not start alone (e.g. from before a restart): ")
                       + "they end themselves after their idle time (idleSecondsLeft). If the account has "
                       "none running, the FitNesse-wide limit is reached: try again later.",
}


_BODY_LOG_BYTES = 64 * 1024  # bodies up to this size are logged whole, larger ones as start and end
_BODY_EXCERPT_CHARS = 200


def _not_a_json_object(
    action: str, status: int, content_type: str, headers, raw_bytes: bytes, text: str, problem: str
) -> ToolError:
    """A JSON answer that isn't a JSON object: say what arrived (bytes against Content-Length, start
    and end) instead of blaming the login, and log the body so the fork team can look at it."""
    length = headers.get("Content-Length") if headers is not None else None
    received = f"{len(raw_bytes)} bytes received" + (f" of Content-Length {length}" if length else "")
    half = _BODY_LOG_BYTES // 2
    logged = raw_bytes if len(raw_bytes) <= _BODY_LOG_BYTES else raw_bytes[:half] + b"\n[...]\n" + raw_bytes[-half:]
    logger.warning(
        "Answer to '%s' (HTTP %s, %s, %s) is not a JSON object (%s). Body:\n%s",
        action, status, content_type, received, problem, logged.decode("utf-8", errors="replace"),
    )
    n = _BODY_EXCERPT_CHARS
    excerpt = repr(text) if len(text) <= 2 * n else f"{text[:n]!r} … {text[-n:]!r}"
    return ToolError(
        f"FitNesse answered '{action}' with HTTP {status} and {content_type}, but not with a JSON "
        f"object ({problem}; {received}). Body: {excerpt}. The MCP server's log has the body. "
        "Check fitnesse_debug_status to see whether the call took effect."
    )


class _Busy(ToolError):
    """HTTP 423: the test process is still working on another document (e.g. after a 504)."""


_BUSY_WAITS = (2.0, 5.0)  # seconds before the 2nd and 3rd try of a read-only action


class _IncompleteAnswer(ToolError):
    """The connection ended before the whole answer arrived."""


# debugTest/debugTable actions that only read: repeated when their answer ends early.
_READ_ONLY_DEBUG_ACTIONS = frozenset({
    "status", "log", "keywords", "keywordsJson", "definedActions", "definedAction", "dynamicVariables",
    "scope", "liveView", "pageHtml", "source", "suitePages",
})


def _debug_post(
    responder: str,
    page_path: str,
    action: str,
    form: Mapping[str, str | int | bool | None] | None = None,
    timeout_seconds: float | None = 30.0,
    state: _DebugState | None = None,
) -> dict:
    """_debug_post_once; an answer that ends early is fetched again for actions that only read."""
    attempts = _READ_ATTEMPTS if action in _READ_ONLY_DEBUG_ACTIONS else 1
    for attempt in range(1, attempts + 1):
        try:
            return _debug_post_once(responder, page_path, action, form, timeout_seconds, state)
        except _Busy as exc:
            if attempt == attempts:
                raise _Busy(f"{exc} Tried {attempts} times." if attempts > 1 else str(exc)) from exc
            logger.warning("%s (attempt %d of %d)", exc, attempt, attempts)
            time.sleep(_BUSY_WAITS[min(attempt, len(_BUSY_WAITS)) - 1])
        except _IncompleteAnswer as exc:
            logger.warning("%s (attempt %d of %d)", exc, attempt, attempts)
            if attempt == attempts:
                if attempts > 1:
                    raise ToolError(f"{exc} Tried {attempts} times; try again later.") from exc
                raise ToolError(
                    f"{exc} The call may have taken effect anyway: check fitnesse_debug_status "
                    "before repeating it."
                ) from exc
    raise AssertionError("unreachable")


def _debug_post_once(
    responder: str,
    page_path: str,
    action: str,
    form: Mapping[str, str | int | bool | None] | None = None,
    timeout_seconds: float | None = 30.0,
    state: _DebugState | None = None,
) -> dict:
    """POST one action to debugTest/debugTable and return its JSON object.

    Raises ToolError for transport errors, non-JSON answers (login problems)
    and HTTP errors; 409 noSession/replaced also forget the session.
    """
    base_url = _base_url()
    username, password = _credentials()
    url = _build_url(base_url, page_path, {"responder": responder})
    fields: dict[str, str] = {"action": action}
    for key, value in (form or {}).items():
        if value is None:
            continue
        fields[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    req = request.Request(url=url, data=parse.urlencode(fields).encode(), method="POST")
    _apply_basic_auth(req=req, username=username, password=password)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    # Without it an expired SSO login answers each request with a redirect plus state cookie.
    req.add_header("X-Requested-With", "XMLHttpRequest")
    if state is not None and state.cookie:
        req.add_header("Cookie", state.cookie)

    try:
        try:
            response = request.urlopen(req, timeout=timeout_seconds)
        except HTTPError as exc:
            response = exc
        with response:
            status = response.status if not isinstance(response, HTTPError) else response.code
            headers = response.headers
            raw_bytes = _read_body(response, DEBUG_MAX_BYTES + 1)
    except TimeoutError as exc:
        if state is not None and action not in _READ_ONLY_DEBUG_ACTIONS:
            state.maybe_busy = True
        raise ToolError(
            f"No answer to '{action}' within {timeout_seconds}s. The test process may still be "
            "running it: check fitnesse_debug_status, which also shows the log."
        ) from exc
    except URLError as exc:
        raise ToolError(f"Connection error: {exc.reason}") from exc
    except _CLOSED as exc:
        raise _IncompleteAnswer(f"{_closed_error(exc)} to '{action}'.") from exc

    if state is not None and headers is not None:
        for header in headers.get_all("Set-Cookie") or []:
            pair = header.split(";", 1)[0].strip()
            if pair.startswith(f"{_DEBUG_COOKIE}="):
                state.cookie = pair
    missing = _missing_bytes(headers, raw_bytes, DEBUG_MAX_BYTES + 1)
    if missing:
        raise _IncompleteAnswer(f"{missing} of the answer to '{action}'.")
    if len(raw_bytes) > DEBUG_MAX_BYTES:
        logger.warning("Response from %s exceeds FITNESSE_DEBUG_MAX_RESPONSE_BYTES=%d", url, DEBUG_MAX_BYTES)
        raise ToolError(
            f"Response to '{action}' exceeds FITNESSE_DEBUG_MAX_RESPONSE_BYTES={DEBUG_MAX_BYTES}."
        )
    content_type = (headers.get("Content-Type", "") if headers is not None else "") or ""
    data = None
    if "json" in content_type:
        text = raw_bytes.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
            problem = f"parsed as {type(data).__name__}, not an object"
        except json.JSONDecodeError as exc:
            problem = f"invalid JSON: {exc}"
        if not isinstance(data, dict):
            raise _not_a_json_object(action, status, content_type, headers, raw_bytes, text, problem)
    if not isinstance(data, dict):
        raise ToolError(
            f"FitNesse answered '{action}' with HTTP {status} and {content_type or 'no content type'} "
            "instead of JSON. Most likely the login is missing, expired or wrong (set "
            "FITNESSE_USERNAME/FITNESSE_PASSWORD or the X-FitNesse-* headers; Basic Auth bypasses the SSO), or this FitNesse "
            f"has no {responder} responder."
        )
    if state is not None and status == 504:
        state.maybe_busy = True
    elif state is not None and status < 400 and action not in _READ_ONLY_DEBUG_ACTIONS:
        state.maybe_busy = False  # it answered: nothing left running
    if status >= 400:
        code = data.get("code")
        message = data.get("error") or f"HTTP {status}"
        hint = _DEBUG_HINTS.get(status, "")
        if status == 409 and code in {"noSession", "replaced"} and state is not None:
            had_session = bool(state.session)
            state.reset()
            hint = (
                "The debug session was replaced by a new one (on the same page; on older forks anywhere), "
                "e.g. from a browser with the same account. This MCP server forgot it; start a new one "
                "with fitnesse_debug_start."
                if code == "replaced"
                else _LOST_HINTS["ended"] + " This MCP server forgot it."
                if had_session
                else "No debug session is running; start one with fitnesse_debug_start."
            )
        elif code == "changed" and action == "definedAction":
            page = str(data.get("page") or "").lstrip(".") or "<page>"
            hint = (
                f"Reload: fitnesse_debug_run_wiki('|define actions at|.{page}|'), then "
                "fitnesse_debug_defined_action_source again."
            )
        elif code in _DEBUG_CODE_HINTS:
            hint = _DEBUG_CODE_HINTS[code]
        elif status == 409 and responder == "debugTable":
            hint = "The page changed since 'original' was read. Read fitnesse_table_source again and retry."
        detail = f" ({code})" if code else ""
        raise (_Busy if status == 423 else ToolError)(f"HTTP {status}{detail}: {message}. {hint}".strip())
    return _without_secrets(data)


# ---- HTML tables ----------------------------------------------------------

_CELL_STATES = ("pass", "fail", "error", "ignore")


_FOLDING_CLASSES = {"collapsible", "included"}  # FitNesse's collapsible sections, FitLibrary's included/"show after"
_FOLDING_TITLE = {("p", "title"), ("span", "meta")}
_DATA_IMAGE = re.compile(r"data:image/([\w.+-]+);base64,(.*)", re.S)
DEBUG_MAX_IMAGES = 4  # inline screenshots attached per tool result


class _TableParser(HTMLParser):
    """Collect the top-level <table> elements of an HTML document.

    Per table: its source span [start, end), its rows of cells, each cell with its text (nested
    tables flattened into it) and FitLibrary status, the images and links inside it, and the
    folding areas that follow it outside any table (e.g. FitLibrary's "show after", logs).
    Images become "[image N]" in the text; N counts the document's images from 1.
    """

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self._source = source
        self._line_starts = [0]
        for i, ch in enumerate(source):
            if ch == "\n":
                self._line_starts.append(i + 1)
        self.tables: list[dict] = []
        self.leading: dict = {"media": [], "after": []}  # before the first table
        self.images: list[dict] = []
        self._depth = 0
        self._cell: dict | None = None
        self._skip = 0  # inside <script>/<style>
        self._fold: dict | None = None  # open folding area outside tables
        self._link: dict | None = None
        self._divs: list[dict] = []  # open <div>s outside tables; includes carry their page
        self._title = False  # inside a collapsible's <p class="title">
        self.feed(source)
        self.close()

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _owner(self) -> dict:
        return self.tables[-1] if self.tables else self.leading

    def _text(self, text: str) -> None:
        if self._link is not None:
            self._link["parts"].append(text)
        if self._cell is not None:
            self._cell["parts"].append(text)
        elif self._fold is not None and self._depth == 0:
            self._fold["title" if self._fold["title_tag"] else "parts"].append(text)

    def _where(self) -> dict:
        """Row and column (1-based) of the cell being read, if any."""
        if self._cell is None:
            return {}
        row = self.tables[-1]["rows"][-1]
        return {"row": len(self.tables[-1]["rows"]), "column": len(row)}

    def _media(self, tag: str, attrs: dict) -> None:
        if tag == "img":
            src = attrs.get("src") or ""
            if not src or "/files/images/collapsable" in src or "/files/fitnesse/images/" in src:
                return
            image = {"n": len(self.images) + 1, "src": src, "alt": attrs.get("alt") or "", **self._where()}
            self.images.append(image)
            self._owner()["media"].append(image)
            self._text(f" [image {image['n']}] ")
        elif tag == "a":
            href = attrs.get("href") or ""
            if href and not href.startswith(("javascript:", "#")):
                self._link = {"href": href, "parts": [], **self._where()}
                self._owner()["media"].append(self._link)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in {"img", "a"}:
            if tag == "a" and self._title and "edit" in classes and self._divs and self._divs[-1]["collapsible"]:
                # an included page's title links to its edit page: /Path?edit...
                self._divs[-1]["include"] = (attrs.get("href") or "").split("?")[0].strip("/") or None
            self._media(tag, attrs)
        elif tag == "table":
            self._depth += 1
            if self._depth == 1:
                include = next((d["include"] for d in reversed(self._divs) if d["include"]), None)
                outer = next((d["include"] for d in self._divs if d["include"]), None)
                self.tables.append({"start": self._offset(), "end": None, "rows": [], "media": [], "after": [],
                                    "include": include, "outer_include": outer})
                if self._fold is not None:
                    self._fold["tables"] = True  # an included page, not a folding area after a table
            else:
                self._text("\n")
        elif tag == "tr":
            if self._depth == 1:
                self.tables[-1]["rows"].append([])
            else:
                self._text("\n")
        elif tag in {"td", "th"}:
            if self._depth == 1 and self.tables and self.tables[-1]["rows"]:
                status = next((c for c in classes if c in _CELL_STATES), None)
                self._cell = {"parts": [], "status": status, "nested": set()}
                self.tables[-1]["rows"][-1].append(self._cell)
            else:
                # a cell of a nested table (e.g. FitLibrary's "Result of then"): keep its status inline
                status = next((c for c in classes if c in _CELL_STATES), None)
                self._text(f" | [{status}] " if status else " | ")
                if status and self._cell is not None:
                    self._cell["nested"].add(status)
        elif tag == "div" and self._depth == 0:
            self._divs.append({"collapsible": "collapsible" in classes, "include": None})
            if self._fold is not None:
                self._fold["divs"] += 1
                self._text("\n")
            elif classes & _FOLDING_CLASSES:
                self._fold = {"divs": 1, "title": [], "parts": [], "title_tag": None, "owner": self._owner()}
        elif tag in {"br", "hr", "p", "div", "li", "pre"}:  # FitLibrary's [fail]: expected <hr> actual
            self._text("\n")
        if tag == "p" and self._depth == 0 and "title" in classes:
            self._title = True
        if self._fold is not None and self._depth == 0 and any((tag, c) in _FOLDING_TITLE for c in classes):
            self._fold["title_tag"] = tag

    def handle_startendtag(self, tag, attrs):
        if tag in {"img", "a"}:
            self._media(tag, dict(attrs))
            self._link = None
        elif tag in {"br", "hr"}:
            self._text("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            self._link = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                close = self._source.find(">", self._offset())
                self.tables[-1]["end"] = len(self._source) if close < 0 else close + 1
                self._cell = None
            else:
                self._text("\n")
        elif tag in {"td", "th"} and self._depth == 1:
            self._cell = None
        if tag == "p" and self._depth == 0:
            self._title = False
        if tag == "div" and self._depth == 0 and self._divs:
            self._divs.pop()
        if tag in {"script", "style", "a", "table", "td", "th"}:
            pass
        elif self._fold is not None and self._depth == 0:
            if tag == self._fold["title_tag"]:
                self._fold["title_tag"] = None
            elif tag == "div":
                self._fold["divs"] -= 1
                if self._fold["divs"] == 0:
                    fold, self._fold = self._fold, None
                    if not fold.get("tables"):
                        fold["owner"]["after"].append({"title": "".join(fold["title"]), "text": "".join(fold["parts"])})

    def handle_data(self, data):
        if not self._skip:
            self._text(data)


def _parse_tables(html: str) -> list[dict]:
    return _parse_document(html)["tables"]


def _parse_document(html: str) -> dict:
    """Tables (see _TableParser), what comes before the first one, and all images in order."""
    parser = _TableParser(html or "")
    for table in parser.tables:
        if table["end"] is None:  # unclosed
            table["end"] = len(html)
        table["rows"] = [
            [{"text": "".join(c["parts"]), "status": c["status"], **({"nested": c["nested"]} if c["nested"] else {})}
             for c in row]
            for row in table["rows"]
        ]
    for owner in (parser.leading, *parser.tables):
        for item in owner["media"]:
            if "parts" in item:  # link
                item["text"] = " ".join("".join(item.pop("parts")).split())
    return {"tables": parser.tables, "leading": parser.leading, "images": parser.images}


def _squash(text: str, limit: int | None = None) -> str:
    lines = [" ".join(line.split()) for line in text.strip().splitlines()]
    text = "\n".join(line for line in lines if line)
    limit = DEBUG_MAX_CELL_CHARS if limit is None else limit
    if limit > 0 and len(text) > limit:
        text = f"{text[:limit]}… [{len(text) - limit} chars cut]"
    return text


def _extras_lines(owner: dict, label: str) -> list[str]:
    """Folding areas, links and non-inline images of a table (or of the text before the first)."""
    lines = []
    for fold in owner["after"]:
        text = _squash(fold["text"])
        if text:
            title = _squash(fold["title"], 200)
            lines.append(f"{label} – after{': ' + title if title else ''}:")
            lines.extend(f"  {line}" for line in text.splitlines())
    for item in owner["media"]:
        where = f" in row {item['row']}, column {item['column']}" if "row" in item else ""
        if "href" in item:
            lines.append(f"{label} – link{where}: {item['text'] or item['href']} -> {item['href']}")
        elif not _DATA_IMAGE.match(item["src"]):
            alt = f" ({item['alt']})" if item["alt"] else ""
            lines.append(f"{label} – image {item['n']}{where}: {item['src']}{alt}")
    return lines


def _failed(table: dict) -> bool:
    """A [fail] or [error] cell, also inside a nested table (e.g. the result of a condition's then)."""
    return any(
        cell["status"] in ("fail", "error") or cell.get("nested", set()) & {"fail", "error"}
        for row in table["rows"] for cell in row
    )


def _first_line(cell: dict) -> str:
    return _squash(cell["text"], 0).split("\n", 1)[0].strip().lower()


def _condition_outcome(table: dict) -> str | None:
    """What a FitLibrary |condition| table did. Only an appended "Result of then"/"Result of else"
    row shows that a branch ran; nothing marks the condition's value itself."""
    rows = [row for row in table["rows"] if row]
    # the header cell carries the error message after the keyword: "condition<hr/> if statement…"
    if not rows or (_first_line(rows[0][0]).split() or [""])[0] != "condition":
        return None
    firsts = [_first_line(row[0]) for row in rows]
    if any(f.startswith("result of then") for f in firsts):
        return "then ran"
    if any(f.startswith("result of else") for f in firsts):
        return "else ran"
    if rows[0][0]["status"] == "error":
        return "condition error, then and else did not run"
    cells = [cell for row in rows for cell in row]
    if all(cell["status"] == "ignore" for cell in cells):
        return "skipped, not run"
    if not any(cell["status"] for cell in cells):
        return "condition false, then did not run"
    return None


def _tables_text(
    html: str, document: dict | None = None, only_failures: bool = False, numbers: list[int] | None = None
) -> str:
    """_table_blocks as one text."""
    return "\n\n".join(text for _, text in _table_blocks(html, document, only_failures, numbers))


def _table_blocks(
    html: str, document: dict | None = None, only_failures: bool = False, numbers: list[int] | None = None
) -> list[tuple[int | None, str]]:
    """Condense annotated FitLibrary HTML into one text block per table: (number, text); the
    number is None for what comes before the first table.

    Cells carry their status as a prefix: [pass], [fail], [error], [ignore]. Folding areas,
    links and images follow the table they belong to. only_failures keeps just the tables with
    a [fail] or [error] cell, under their usual numbers. numbers: the page's indices of the tables
    that ran (run_page with tables), used as the tables' numbers when there is one per table.
    """
    document = document or _parse_document(html)
    if not numbers or len(numbers) != len(document["tables"]):
        numbers = list(range(len(document["tables"])))
    blocks = []
    leading = [] if only_failures else _extras_lines(document["leading"], "Before the first table")
    if leading:
        blocks.append((None, "\n".join(leading)))
    for i, table in zip(numbers, document["tables"]):
        if only_failures and not _failed(table):
            continue
        lines = [f"Table {i}:"]
        for row in table["rows"]:
            cells = []
            for cell in row:
                text = _squash(cell["text"]).replace("\n", " ⏎ ")
                cells.append(f"[{cell['status']}] {text}" if cell["status"] else text)
            lines.append("| " + " | ".join(cells) + " |")
        outcome = _condition_outcome(table)
        if outcome:
            lines.append(f"Table {i} – condition: {outcome}")
        lines.extend(_extras_lines(table, f"Table {i}"))
        blocks.append((i, "\n".join(lines)))
    return blocks


def _inline_images(document: dict) -> list[dict]:
    """The document's data: images, decoded: n, format, data (None if not decodable)."""
    images = []
    for image in document["images"]:
        match = _DATA_IMAGE.match(image["src"])
        if not match:
            continue
        try:
            data = b64decode("".join(match.group(2).split()), validate=True) or None
        except (binascii.Error, ValueError):
            data = None
        images.append({"n": image["n"], "format": match.group(1), "data": data})
    return images


_IMAGE_SUFFIXES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}


def _files_link(url: str) -> str:
    """The path of a link into FitNesse's files section (/files/...) from a debug result: a path
    or a URL on this FitNesse. Anything else is refused: the tools read only FitNesse's files."""
    link = url.strip()
    base = _base_url().rstrip("/")
    if link.startswith(base + "/"):
        link = link[len(base):]
    parts = parse.urlsplit(link)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        raise ToolError(f"Not a link into this FitNesse's files section: {url!r}. Pass the /files/... "
                        "link from a debug result.")
    path = parse.unquote(parts.path)
    if not path.startswith("/files/") or ".." in path.split("/"):
        raise ToolError(f"Not a link into this FitNesse's files section: {url!r}. Pass the /files/... "
                        "link from a debug result.")
    return path


def _image_format(path: str) -> str | None:
    return _IMAGE_SUFFIXES.get(path[path.rfind("."):].lower()) if "." in path else None


def _failure_screenshot(html: str) -> list[dict]:
    """The screenshot KDTA links from the failing (last) table of a run (e.g. 'Screen' ->
    /files/screenCaptures/....png), fetched for attaching. Best effort: [] if none or unreadable."""
    tables = _parse_document(html)["tables"]
    if not tables:
        return []
    links = [m["href"] for m in tables[-1]["media"] if "href" in m]
    for href in reversed(links):
        try:
            path = _files_link(href)
        except ToolError:
            continue
        fmt = _image_format(path)
        if fmt is None:
            continue
        got = _request_bytes("GET", path, timeout_seconds=30.0)
        if not got.get("ok") or got.get("truncated"):
            logger.info("screenshot %s not attached: %s", path, got.get("error") or "too large")
            return []
        return [{"link": path, "format": fmt, "data": got["body"] or None}]
    return []


_HIDDEN_HTML = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.S | re.I)


def _link_text(body: bytes, content_type: str) -> str:
    """A linked text file for reading: HTML without scripts, styles and comments; whitespace squeezed."""
    match = re.search(r"charset=([\w.-]+)", content_type or "")
    try:
        text = body.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    if "html" in (content_type or "").lower() or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        text = _HIDDEN_HTML.sub(lambda m: f"<{m.group(1)}/>" if m.group(1) else "", text)
    return re.sub(r"\s+", " ", text).strip()


def _snippets(text: str, needle: str, context: int, limit: int) -> tuple[int, list[dict]]:
    """Every place text contains needle (case-insensitive), with context around; overlapping
    places merged. Returns the number of places and at most limit snippets."""
    low, wanted = text.lower(), needle.lower()
    spans: list[list[int]] = []
    places = 0
    pos = low.find(wanted)
    while pos != -1:
        places += 1
        start, end = max(0, pos - context), min(len(text), pos + len(wanted) + context)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = end
        else:
            spans.append([start, end])
        pos = low.find(wanted, pos + len(wanted))
    return places, [
        {"at": a, "text": ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")}
        for a, b in spans[:limit]
    ]


_IMAGE_PARTS = ("suite_setup", "previous_page_teardown", "teardown", "teardown_tables", "suite_teardown", "end")


def _image_parts(part: dict):
    """The document parts of a result, nested ones first, the result itself last."""
    for key in _IMAGE_PARTS:
        if isinstance(part.get(key), dict):
            yield from _image_parts(part[key])
    yield part


def _with_images(result: dict) -> dict | ToolResult:
    """Attach the inline images of result (and its tear-down parts) as MCP image content.

    Each document part lists its images in "images" with the content index they got, or why not.
    """
    attached: list[Image] = []
    for part in _image_parts(result):
        listed = []
        for image in part.pop("_images", []):
            entry = ({"link": image["link"]} if "link" in image else {"image": image["n"]}) | {"format": image["format"]}
            if image["data"] is None:
                entry["omitted"] = "empty" if "link" in image else "invalid base64"
            elif len(attached) >= DEBUG_MAX_IMAGES:
                entry["omitted"] = f"more than {DEBUG_MAX_IMAGES} images in this result; use include_html"
            else:
                attached.append(Image(data=image["data"], format=image["format"]))
                entry["content"] = len(attached)  # 1-based, after the text block
            listed.append(entry)
        if listed:
            part["images"] = listed
    if not attached:
        return result
    content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    content.extend(image.to_image_content() for image in attached)
    return ToolResult(content=content, structured_content=result)


def _query_rows(html: str) -> list[list[str]]:
    """Text cells (unsquashed) of the first table of a query answer."""
    tables = _parse_tables(html)
    if not tables:
        return []
    return [[cell["text"].strip() for cell in row] for row in tables[0]["rows"]]


def _query_records(html: str, kind: str) -> list[dict[str, str]]:
    """Header row plus data rows to dicts; drops the empty marker column."""
    rows = _query_rows(html)
    if not rows:
        raise ToolError(f"The '{kind}' answer contains no table.")
    header, *data = rows
    names = header[1:]
    return [dict(zip(names, row[1:])) for row in data]


# ---- Session handling -----------------------------------------------------


def _require_session() -> _DebugState:
    state = _debug_state()
    if not state.session:
        raise ToolError(
            "This MCP server has no debug session on this FitNesse. Start one with fitnesse_debug_start"
            + (", or continue a running one with fitnesse_debug_status(adopt=true)." if ALLOW_ADOPT else ".")
        )
    return state


def _session_action(
    state: _DebugState,
    action: str,
    form: Mapping[str, str | int | bool | None] | None = None,
    timeout_seconds: float | None = 30.0,
) -> dict:
    if not state.session:  # lost in this call (409): without an id FitNesse picks the account's session
        raise ToolError("This MCP server's debug session has ended. Start a new one with fitnesse_debug_start.")
    if action == "runTable":
        state.table_started = time.time() * 1000
    return _debug_post(
        "debugTest",
        state.page or "",
        action,
        {"session": state.session, **(form or {})},
        timeout_seconds=timeout_seconds,
        state=state,
    )


class _document:
    """Documents of one session run one after the other: fail fast instead of queueing."""

    def __init__(self, state: _DebugState):
        self._lock = state.lock

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            raise ToolError(
                "Another document is still running in this debug session. Wait for it "
                "(fitnesse_debug_status shows progress) or call fitnesse_debug_end with abort=true."
            )

    def __exit__(self, *_):
        self._lock.release()


def _add_counts(state: _DebugState, page: str | None, counts: Mapping | None) -> dict[str, int]:
    totals = state.page_counts.setdefault(page or "", {k: 0 for k in ("right", "wrong", "ignores", "exceptions")})
    for key, value in (counts or {}).items():
        if isinstance(value, int):
            totals[key] = totals.get(key, 0) + value
    return dict(totals)


def _document_result(
    state: _DebugState, data: dict, page: str | None, include_html: bool, only_failures: bool = False,
    numbers: list[int] | None = None,
) -> dict:
    html = data.get("html") or ""
    document = _parse_document(html)
    result = {
        "page": page,
        "counts": data.get("counts"),
        "page_totals": _add_counts(state, page, data.get("counts")),
    }
    if text := _tables_text(html, document, only_failures, numbers):
        result["tables"] = text
    if only_failures:
        passed = sum(1 for t in document["tables"] if not _failed(t))
        if passed:
            result["tables_without_failures"] = passed
    images = _inline_images(document)
    if images:
        result["_images"] = images  # attached by _with_images
    if include_html:
        result["html"] = html
    return result


def _covers_setup(html: str, tables: list[int]) -> bool:
    """tables include every SetUp table of the page (run_page(tables=[...]) that ran the SetUp)."""
    setup = _setup_indices(_parse_tables(html))
    return bool(setup) and set(setup) <= set(tables)


def _with_setup_hint(state: _DebugState, result: dict, timeout_seconds: float | None) -> dict:
    """run_wiki before the SetUp: a failure there is often a defined action, browser or fixture
    the SetUp would have provided. Only for pages that have a SetUp."""
    counts = result.get("counts") or {}
    page = state.current_page or state.page
    if not (counts.get("wrong") or counts.get("exceptions")):
        return result
    if state.suite_setup and (not state.frame_started or state.suite_setup_pending):
        return {**result, "hint": (
            f"The SuiteSetUp ({state.suite_setup}) has not run in this session, so the browser, "
            "defined actions and fixtures it provides may be missing: run "
            "fitnesse_debug_run_page(setup_only=true) (runs the SuiteSetUp and the page's SetUp), "
            "then these tables again."
        )}
    if not page or state.setup_seen == page:
        return result
    try:
        has_setup = bool(_setup_indices(_parse_tables(_page_html(state, page, timeout_seconds))))
    except ToolError:
        return result  # the hint is optional
    if not has_setup:
        state.setup_seen = page  # nothing to run: don't ask again in this test
        return result
    return {**result, "hint": (
        f"The SetUp of {page} has not run in this test, so defined actions, the browser and "
        "fixtures it provides may be missing: run fitnesse_debug_run_page(setup_only=true), then "
        "these tables again."
    )}


class _TearDownFailed(ToolError):
    """The TearDown tables could not run (not: ran with failures); the session is still there."""


def _seen_page(state: _DebugState, parsed: list[dict], ran: list[int]) -> None:
    """After tables of current_page ran: note them and where its TearDown is (see _end_test)."""
    state.ran_tables.update(ran)
    state.teardown_tables = _teardown_indices(parsed)


def _run_teardown(state: _DebugState, page: str, timeout_seconds: float | None) -> dict | None:
    """Run page's TearDown tables that have not run in its test, in one go; their result."""
    html_page = _page_html(state, page, timeout_seconds)  # fresh: it may have been edited
    parsed = _parse_tables(html_page)
    todo = [i for i in _teardown_indices(parsed) if i not in state.ran_tables]
    if not todo:
        return None
    html = "\n".join(html_page[parsed[i]["start"]:parsed[i]["end"]] for i in todo)
    data = _session_action(state, "runTable", {"html": html}, timeout_seconds=timeout_seconds)
    state.ran_tables.update(todo)
    result = _document_result(state, data, page, include_html=False, numbers=todo)
    del result["page"]
    return result


def _end_test(state: _DebugState, skip_teardown: bool, timeout_seconds: float | None) -> dict:
    """Before nextPage or end, which run no TearDown tables: the current page's TearDown tables
    that have not run in its test, as a normal test run would. Only if run_page ran some of the
    page's tables (not after run_wiki alone). Returns teardown_tables (their result),
    teardown_skipped and tables_not_run (tables a stop left out), for the report of the test's end."""
    page = state.current_page
    report: dict = {}
    pending = sorted(set(state.teardown_tables) - state.ran_tables)
    if page and state.ran_tables and pending:
        if skip_teardown:
            report["teardown_skipped"] = pending
        else:
            try:
                tables = _run_teardown(state, page, timeout_seconds)
            except ToolError as exc:
                if not state.session:  # the session is gone: nothing to skip
                    raise
                raise _TearDownFailed(f"The TearDown of {page} could not run: {exc}") from exc
            if tables is not None:
                report["teardown_tables"] = tables
    if state.table_count is not None:  # a whole-page run stopped and was not continued
        left = [i for i in range(state.next_table, state.table_count) if i not in state.ran_tables]
        if left:
            report["tables_not_run"] = len(left)
    return report


def _next_page(
    state: _DebugState, page: str | None, timeout_seconds: float | None, skip_teardown: bool = False
) -> dict:
    previous = state.current_page
    before = _end_test(state, skip_teardown, timeout_seconds)
    data = _session_action(state, "nextPage", {"page": page}, timeout_seconds=timeout_seconds)
    teardown = _document_result(state, data, previous, include_html=False)
    teardown["teardown_of"] = teardown.pop("page")
    teardown.update(before)
    state.current_page = _page_key(data.get("page")) or _page_key(page) or previous
    state.page_counts.pop(state.current_page, None)
    state.setup_ran = state.setup_seen = None  # a new test begins: its SetUp has not run
    state.next_table, state.table_count = 0, None
    state.ran_tables, state.teardown_tables = set(), []
    return teardown


def _sessions(status: dict) -> list[dict] | None:
    """The account's running sessions from status (mod #159), pages without leading dot; None on
    older forks, which allow one session per account."""
    sessions = status.get("sessions")
    if not isinstance(sessions, list):
        return None
    return [{**s, "session": str(s.get("session")), "page": str(s.get("page") or "").lstrip(".")}
            for s in sessions if isinstance(s, dict)]


def _session_list(sessions: list[dict]) -> str:
    return ", ".join(f"{s['session']} on {s['page']}" for s in sessions)


def _check_start(page_path: str, sessions: list[dict], state: _DebugState, replace: bool) -> None:
    """Refuse a start that would replace a session on this page, or leave this server's own running."""
    page = _page_key(page_path)
    here = next((s for s in sessions if s["page"] == page), None)
    own = next((s for s in sessions if state.session and s["session"] == state.session), None)
    if own is not None and own is not here:
        raise ToolError(
            f"This MCP server's debug session {own['session']} is still running on {own['page']}. "
            "End it with fitnesse_debug_end first (this server works with one session at a time), "
            "or continue it."
        )
    if here is not None and here is own and not replace:
        raise ToolError(
            f"This MCP server's debug session is already running on {page}: keep using it "
            "(fitnesse_debug_run_page / _run_wiki, restart=true for a fresh test), or end it with "
            "fitnesse_debug_end."
        )
    if here is not None and not replace:
        raise ToolError(
            f"A debug session started by someone using the same account (maybe in a browser) is "
            f"already running on {page} (session "
            f"{here['session']}, started {_clock(here.get('startedAt'), with_date=True)}). " + _foreign_session_advice(here["session"])
            + " Sessions on other pages are not affected."
        )


def _foreign_session_advice(session: str | None) -> str:
    """What to do about a running session this server did not start."""
    if ALLOW_ADOPT:
        which = f", session='{session}'" if session else ""
        return (
            f"Continue it with fitnesse_debug_status(adopt=true{which}), end it, or pass replace=true to "
            "kill it without tear down (its browser tab then shows 'replaced')."
        )
    return (
        "This server does not take over sessions it did not start (FITNESSE_ALLOW_ADOPT is off). Work "
        "on another page, or wait: it ends itself after its idle time (idleSecondsLeft in "
        "fitnesse_debug_status). Pass replace=true only when told to: it kills the session without "
        "tear down (its browser tab then shows 'replaced')."
    )


def _session_to_adopt(status: dict, sessions: list[dict] | None, wanted: str | None) -> dict | None:
    """The session status(adopt=true) takes over: the one named, else the one status describes."""
    if wanted:
        if str(status.get("session")) == wanted and status.get("running"):
            return status
        found = next((s for s in sessions or [] if s["session"] == wanted), None)
        if found is None:
            running = _session_list(sessions) if sessions else "none"
            raise ToolError(f"No running debug session {wanted} of this account (running: {running}).")
        return found
    if status.get("running"):
        return status
    if sessions:
        raise ToolError(
            f"Several debug sessions of this account are running, none on this page: "
            f"{_session_list(sessions)}. Pass session=<id> (or page_path) to choose one."
        )
    return None


def _status(
    page_path: str, state: _DebugState, timeout_seconds: float | None, with_session: bool = True
) -> dict:
    form = {"session": state.session} if state.session and with_session else None
    return _debug_post("debugTest", page_path, "status", form, timeout_seconds=timeout_seconds, state=state)


def _session_busy(state: _DebugState, timeout_seconds: float | None) -> bool:
    """Whether this server's session still runs a document that timed out (only asked after one
    did: a session answers definedAction(s) only after the document, as the fork's UI knows)."""
    if not state.maybe_busy or not state.session:
        return False
    try:
        status = _status(state.page or "FrontPage", state, timeout_seconds)
    except ToolError as exc:
        logger.info("status not read: %s", exc)
        return False
    mine = [s for s in _sessions(status) or [] if s.get("session") == state.session]
    busy = bool(mine[0].get("busy")) if mine else bool(status.get("busy"))
    state.maybe_busy = busy
    return busy


_LOST_HINTS = {
    "replaced": "A new session on the same page replaced it (another client or a browser with this "
                "account). If you still need one, check 'sessions' and start a new one with fitnesse_debug_start.",
    "ended": "It ended: after its idle time, by fitnesse_debug_end/abort from another client with this "
             "account, or the test process crashed. Start a new one with fitnesse_debug_start if you still need it.",
}


def _lost_session(state: _DebugState, timeout_seconds: float | None) -> dict:
    """Why this server's session is gone: status with its id answers replaced=true if a new session
    replaced it (the fork keeps the last 1000 replaced ids), else it ended."""
    reason = "ended"
    try:
        data = _debug_post("debugTest", state.page or "FrontPage", "status", {"session": state.session},
                           timeout_seconds=timeout_seconds, state=state)
        if data.get("replaced"):
            reason = "replaced"
    except ToolError as exc:
        logger.info("status of the lost session not read: %s", exc)
    return {"session": state.session, "page": state.page, "reason": reason, "hint": _LOST_HINTS[reason]}


def _split_tables(html: str) -> list[str]:
    return [html[t["start"]:t["end"]] for t in _parse_tables(html)]


_SETUP_PAGES = {"SetUp", "SuiteSetUp"}  # the only pages FitNesse includes as set up


def _setup_indices(tables: list[dict]) -> list[int]:
    """The SetUp's tables: those whose outermost include is a SetUp/SuiteSetUp page, including the
    pages that SetUp includes itself. A plain !include at the top of a page is not part of it."""
    return [
        i for i, table in enumerate(tables)
        if (table.get("outer_include") or "").rsplit(".", 1)[-1] in _SETUP_PAGES
    ]


def _teardown_indices(tables: list[dict]) -> list[int]:
    """The TearDown's tables, as _setup_indices. SuiteTearDown is not in pageHtml (withSuiteSetUp
    false): this server runs it as a page of its own."""
    return [
        i for i, table in enumerate(tables)
        if (table.get("outer_include") or "").rsplit(".", 1)[-1] == "TearDown"
    ]


def _table_sources(parsed: list[dict], page: str | None) -> list[tuple[str | None, int]]:
    """Per table of a run: the page it is written on and its index there (fitnesse_table_* tools)."""
    own: dict[str | None, int] = {}
    sources = []
    for table in parsed:
        source_page = table["include"] or page
        own[source_page] = own.get(source_page, -1) + 1
        sources.append((source_page, own[source_page]))
    return sources


_TEXT_BEFORE_CHARS = 600  # page text before a failing table, kept from its end
_BLOCK_END = re.compile(r"<br\s*/?>|</(?:p|div|h[1-6]|li|tr|pre)\s*>", re.I)


def _html_text(fragment: str) -> str:
    """Rendered wiki HTML as plain text: scripts/styles dropped, tags removed, one line per block."""
    text = _HIDDEN_HTML.sub("", fragment)
    text = re.sub(r"<a\b[^>]*class=\"edit\"[^>]*>.*?</a>", "", text, flags=re.S | re.I)  # include titles
    text = _BLOCK_END.sub("\n", text)
    text = _unescape_html(re.sub(r"<[^>]+>", " ", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _stop_details(
    state: _DebugState, html_page: str, parsed: list[dict], page: str | None, failed: int,
    timeout_seconds: float | None,
) -> dict:
    """stopped_at for the failing table: its index, where it is written (source_page,
    source_index), the page text just before it (text_before, from the rendered page) and its
    wiki (from debugTable source of source_page, to change and try with run_wiki). Best effort:
    text_before and wiki are left out when there is none or it can't be read. log: the
    warnings, errors and browser console entries written since the table began (_stop_log)."""
    source_page, source_index = _table_sources(parsed, page)[failed]
    details: dict = {"index": failed, "source_page": source_page, "source_index": source_index}
    start = parsed[failed - 1]["end"] if failed else 0
    before = _html_text(html_page[start:parsed[failed]["start"]])
    if before:
        details["text_before"] = (
            "…" + before[-_TEXT_BEFORE_CHARS:] if len(before) > _TEXT_BEFORE_CHARS else before)
    try:
        source = _debug_post("debugTable", source_page or "", "source", timeout_seconds=timeout_seconds or 30.0)
        tables = source.get("tables") if isinstance(source, dict) else None
        if isinstance(tables, list) and source_index < len(tables) and isinstance(tables[source_index], str):
            details["wiki"] = tables[source_index]
    except ToolError as exc:
        logger.info("wiki of %s table %s not read: %s", source_page, source_index, exc)
    log = _stop_log(state, timeout_seconds)
    if log:
        details["log"] = log
    return details


def _all_passed(data: dict) -> bool:
    """A run with at least one table and no [fail]/[error] cell or wrong/exception count."""
    counts = data.get("counts") if isinstance(data.get("counts"), Mapping) else {}
    tables = _parse_tables(data.get("html") or "")
    return bool(tables) and not counts.get("wrong") and not counts.get("exceptions") and not any(map(_failed, tables))


def _run_tables_stepwise(
    state: _DebugState, parts: list[str], chosen: list[int], timeout_seconds: float | None
) -> tuple[dict, list[int], int | None]:
    """Run the chosen tables one runTable each, as the browser debugger steps, and stop after the
    first with a [fail]/[error] cell or a wrong/exception count. Returns the combined answer (html
    and counts as one runTable would give them), the indices that ran and the failing one."""
    pieces: list[str] = []
    counts: dict[str, int] = {}
    ran: list[int] = []
    for i in chosen:
        data = _session_action(state, "runTable", {"html": parts[i]}, timeout_seconds=timeout_seconds)
        html = data.get("html") or ""
        pieces.append(html)
        ran.append(i)
        step = data.get("counts") if isinstance(data.get("counts"), Mapping) else {}
        for key, value in step.items():
            if isinstance(value, int):
                counts[key] = counts.get(key, 0) + value
        if step.get("wrong") or step.get("exceptions") or any(_failed(t) for t in _parse_tables(html)):
            return {"html": "\n".join(pieces), "counts": counts}, ran, i
    return {"html": "\n".join(pieces), "counts": counts}, ran, None


def _page_html(state: _DebugState, page: str, timeout_seconds: float | None) -> str:
    # withSuiteSetUp=false: since mod #164 pageHtml embeds SuiteSetUp/SuiteTearDown for the start
    # page; this server runs them as pages of their own (_run_suite_setup), as ?test does
    return _session_action(
        state, "pageHtml", {"page": page, "withSuiteSetUp": False}, timeout_seconds=timeout_seconds
    ).get("html") or ""


def _walk_keywords(node, path: tuple[str, ...] = ()):
    if isinstance(node, list):
        for item in node:
            yield from _walk_keywords(item, path)
    elif isinstance(node, dict):
        if "syntax" in node:
            yield path, node
        children = node.get("children")
        if children is not None:
            name = node.get("name") or node.get("label") or node.get("title") or node.get("text")
            sub = path + (str(name),) if name and "syntax" not in node else path
            yield from _walk_keywords(children, sub)


_MANDATORY = "this statement is mandatory"


def _without_help(node):
    """A keyword entry without its help texts; a help text saying the row is mandatory becomes
    mandatory=true."""
    if isinstance(node, list):
        return [_without_help(item) for item in node]
    if not isinstance(node, dict):
        return node
    result = {k: _without_help(v) for k, v in node.items() if k != "helpText"}
    if _MANDATORY in str(node.get("helpText") or "").lower():
        result["mandatory"] = True
    return result


# keywordsJson lists every public method of the classes in scope; 'generated' ones have no keyword
# annotation (also those of classes without any). The session's keywords table knows fewer, so
# only its 'defined' rows are read (defined actions).
# A syntax is an annotation's wiki as written, i.e. help-page markup such as |''<i>name</i>''|%1|.
# FitLibrary reads the tags as part of the name (Missing method …LessThanIGreaterThan…), so they go.
# Empty tags (</>, as in |''with</>''|%2|) go as well.
_SYNTAX_TAGS = re.compile(r"</?(?:i|b|em|strong|u)?>", re.I)
# A CamelCase word such as JavaScript is a WikiWord to FitNesse: in a table it becomes a link and
# FitLibrary looks for executeJavaScriptLeftSquareBracketQuestionRightSquareBracket. !-...-! keeps
# it text. FitNesse's rule: a capital, then lower case letters or digits before each further capital.
_WIKI_WORD = re.compile(r"(?<![\w!])[A-Z](?:[a-z0-9]+[A-Z][a-z0-9]*)+(?!\w)")
_ESCAPED = re.compile(r"(!-.*?-!)", re.S)


# Since lib #92 keywordsJson sends syntax this way (no tags, WikiWords in !-...-!); for older forks
# (tcas-ta-ng runs 1.6.3) it is done here, and an already escaped syntax passes unchanged.
def _escape_wiki_words(wiki: str) -> str:
    """|''execute JavaScript''|%1| -> |''execute !-JavaScript-!''|%1|; text already in !-...-! stays."""
    parts = _ESCAPED.split(wiki)
    return "".join(p if i % 2 else _WIKI_WORD.sub(lambda m: f"!-{m[0]}-!", p) for i, p in enumerate(parts))


def _clean_syntax(entry: dict) -> dict:
    syntax = entry.get("syntax")
    if not isinstance(syntax, str):
        return entry
    return {**entry, "syntax": _escape_wiki_words(_SYNTAX_TAGS.sub("", syntax))}


def _keywords(html: str, filter: str | None, limit: int, details: bool = False) -> dict:
    rows = _query_rows(html)
    cells = rows[0] if rows else []
    try:
        keywords = json.loads(cells[1]) if len(cells) > 1 and cells[1] else []
        special = json.loads(cells[2]) if len(cells) > 2 and cells[2] else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"Unexpected keywordsJson answer: {exc}") from exc
    found = [
        {"category": " / ".join(path), "special": is_special, **_clean_syntax(entry)}
        for is_special, source in ((False, keywords), (True, special))
        for path, entry in _walk_keywords(source)
    ]
    if filter:
        needle = filter.lower()
        hits = [e for e in found if needle in json.dumps(e, ensure_ascii=False).lower()]
        shown = hits[:limit] if details else [_without_help(e) for e in hits[:limit]]
        return {"matches": len(hits), "keywords": shown}
    categories: dict[str, int] = {}
    for entry in found:
        label = entry["category"] + (" (special)" if entry["special"] else "")
        categories[label] = categories.get(label, 0) + 1
    return {
        "count": len(found),
        "categories": categories,
        "hint": "Number of keywords per category. Pass filter (a word such as 'click' or a category "
                "such as 'Web / Browser') to get the matching keywords with syntax and parameters.",
    }


# ---- Defined actions --------------------------------------------------------
# The session's keywords answer (action=keywords, not keywordsJson) lists the plain-text defined
# actions that |define actions at| loaded into FitLibrary's repository, one row each:
# defined | wiki | tooltip | author | defining page | overridden pages.

# FitLibrary's rules for several objects of one class; in the result, as only kind='scope' needs them.
_SCOPE_NOTE = (
    "Lookup order: the table's own object, those of |add global| (level global, newest first), "
    "the selected and select objects, suite, FitLibrary's globals and flow. Several objects of "
    "one class only with |add named|, and only while the class has no global or local object; "
    "after it, |add global| and local objects of that class don't work, and the other way "
    "round. A local object comes before a global one of its class but is gone at the next test."
)
_NO_DEFINED_ACTIONS = (
    "No defined actions loaded in this session. They are loaded by |define actions at|.Page| "
    "tables, usually in SetUp: find them with fitnesse_debug_page_tables and run them with "
    "fitnesse_debug_run_page(tables=[...])."
)
_MARKUP = re.compile(r"</?\w+>|'{2,3}")
# FitLibrary (DefinedActionBodyCollector) ends a definition at an <hr/> between two tables. FitNesse
# renders exactly four dashes as <hr/> anywhere in the text, five or more as <hr size=…/>, and
# nothing inside !-...-!, {{{...}}} or # comment lines. Same as the mod's DefinedActionBlocks.
_SEPARATOR = re.compile(r"(?<!-)----(?!-)")
_NOT_WIKI = re.compile(r"!-.*?-!|\{\{\{.*?\}\}\}|^#[^\n]*", re.S | re.M)  # a comment is one line


def _separates(text: str) -> bool:
    """Whether the text between two tables ends a definition."""
    return bool(_SEPARATOR.search(_NOT_WIKI.sub(" ", text)))


def _plain(text: str) -> str:
    return " ".join(_MARKUP.sub("", text).split())


def _wiki_cells(wiki: str) -> list[str]:
    """Cells of the first row of a wiki table, markup kept."""
    row = next((line.strip() for line in wiki.splitlines() if line.strip().startswith("|")), "")
    return [c.strip() for c in row.strip().strip("|").split("|")] if row else []


def _defined_actions(html: str) -> list[dict]:
    actions = []
    for row in _query_rows(html):
        if not row or row[0] != "defined":
            continue
        wiki, note, author, page, overrides = (row[1:] + [""] * 5)[:5]
        cells = _wiki_cells(wiki)
        actions.append({
            "action": "|" + "|".join(_plain(c) for c in cells) + "|",
            "keywords": [_plain(c) for c in cells if "''" in c],
            "parameters": [_plain(c) for c in cells if "''" not in c],
            "note": note,
            "deprecated": "deprecated" in note.lower(),
            "author": author,
            "page": page.lstrip("."),
            "overrides": [p.strip().lstrip(".") for p in overrides.split(",") if p.strip()],
        })
    return actions


def _json_defined_action(entry: dict) -> dict:
    """An entry of debugTest action=definedActions: action is runnable wiki (|greet|name|), so
    keywords and parameters alternate as FitLibrary reads a row."""
    action = _call_row(str(entry.get("action") or ""))
    cells = _wiki_cells(action)
    return {
        "action": action,
        "keywords": cells[::2],
        "parameters": cells[1::2],
        "note": str(entry.get("note") or ""),
        "deprecated": bool(entry.get("deprecated")),
        "author": str(entry.get("author") or ""),
        "page": str(entry.get("page") or "").lstrip("."),
        "overrides": [str(p).strip().lstrip(".") for p in entry.get("overrides") or [] if str(p).strip()],
    }


def _session_defined_actions(state: _DebugState, timeout_seconds: float | None) -> list[dict]:
    """The session's defined actions: debugTest action=definedActions where the fork has it, else
    parsed from the keywords HTML."""
    try:
        if _session_busy(state, timeout_seconds):  # it would answer only after that document
            raise _Busy("The test process is still running a document that timed out: wait until "
                        "fitnesse_debug_status shows busy=false, or fitnesse_debug_end(abort=true).")
        with _document(state):
            try:
                data = _session_action(state, "definedActions", timeout_seconds=timeout_seconds)
            except ToolError as exc:
                if not _unknown_action(exc):
                    raise
            else:
                actions = [_json_defined_action(a) for a in data.get("definedActions") or [] if isinstance(a, dict)]
                state.defined_actions = actions
                return actions
            data = _session_action(state, "keywords", timeout_seconds=timeout_seconds)
    except _Busy:
        if state.defined_actions is None:
            raise
        logger.info("test process busy: using the defined actions read before")
        return state.defined_actions
    state.defined_actions = _defined_actions(data.get("html") or "")
    return state.defined_actions


def _short_action(action: dict) -> dict:
    """What a reload reports per defined action: the call, plus note/author/deprecated if set."""
    return {"action": action["action"], **{k: action[k] for k in ("note", "author", "deprecated") if action.get(k)}}


def _matches_action(text: str, needle: str | None) -> bool:
    return not needle or needle.lower() in text.lower()


def _definition_block(source: Mapping, action: dict) -> dict:
    """The block of a page that defines action, found as FitLibrary reads the page.

    The header is a table whose first row's keyword cells (every other cell, starting with the
    first) match the action's keywords, and that is the page's first table or comes right after
    a ---- (see _separates). The block runs to the table before the next ----, with the text in
    between.
    """
    tables = list(source.get("tables") or [])
    texts = list(source.get("texts") or [])
    wanted = [k.lower() for k in action["keywords"]]
    starts = [
        i for i, table in enumerate(tables)
        if (i == 0 or _separates(texts[i] if i < len(texts) else ""))
        and [_plain(c).lower() for c in _wiki_cells(table)[::2]] == wanted
    ]
    if not starts:
        raise ToolError(
            f"No definition of {action['action']} found on {action['page']}: the page may have "
            "changed since the session loaded it. Check it with fitnesse_table_source."
        )
    if len(starts) > 1:
        raise ToolError(
            f"{action['action']} is defined {len(starts)} times on {action['page']} (tables "
            f"{starts}): refusing to pick one. Edit the page with fitnesse_table_source / fitnesse_table_edit."
        )
    first = last = starts[0]
    while last + 1 < len(tables) and not _separates(texts[last + 1] if last + 1 < len(texts) else ""):
        last += 1
    original = tables[first]
    for i in range(first + 1, last + 1):
        original += (texts[i] if i < len(texts) else "") + tables[i]
    return {"index": first, "last": last if last > first else None, "original": original}


def _unknown_action(exc: ToolError) -> bool:
    """The fork can't do it: 400 unknownAction (older mods: 400 Unknown action without a code), or
    501 unsupported from a test process with an older FitLibrary."""
    text = str(exc).lower()
    return ("(unknownaction)" in text or "unknown action" in text or "(unsupported)" in text
            or text.startswith("http 501"))


class _LookUpOnPage(ToolError):
    """definedAction gave no block: look the definition up on its page as on older forks (as the UI
    does). reason: 'unknownPlace' (e.g. |define actions at|...|from|...| with another root),
    'changed' (the page changed since the session loaded it) or 'busy' (423)."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def _server_defined_action(call: str, state: _DebugState) -> dict | None:
    """debugTable action=definedAction: the fork picks the definition as a run would (keywords,
    number of arguments, loaded last wins) and cuts its block. None if this fork can't. While the
    session runs a document that timed out it would answer only after that: look it up on the page."""
    if _session_busy(state, 30.0):
        raise _LookUpOnPage("The test process is still running a document that timed out.", "busy")
    try:
        found = _debug_post(
            "debugTable", state.page or "", "definedAction", {"name": call, "session": state.session},
            timeout_seconds=30.0, state=state,
        )
    except _Busy as exc:
        raise _LookUpOnPage(str(exc), "busy") from exc
    except ToolError as exc:
        if _unknown_action(exc):
            return None
        for code in ("unknownPlace", "changed"):
            if f"({code.lower()})" in str(exc).lower():
                raise _LookUpOnPage(str(exc), code) from exc
        raise
    found = {k: found[k] for k in ("action", "page", "index", "last", "original") if k in found}
    if isinstance(found.get("page"), str):
        found["page"] = found["page"].lstrip(".")
    if "action" in found:
        found["action"] = _call_row(found["action"])
    return found


def _call_row(wiki: str) -> str:
    """|''<i>greet</i>''|name| -> |greet|name|"""
    return "|" + "|".join(_plain(c) for c in _wiki_cells("|" + wiki.strip().strip("|") + "|")) + "|"


def _session_defined_action(state: _DebugState, action: str, timeout_seconds: float | None) -> dict:
    actions = _session_defined_actions(state, timeout_seconds)
    if not actions:
        raise ToolError(_NO_DEFINED_ACTIONS)
    if action.strip().startswith("|"):  # a call row: keywords and number of arguments, as FitLibrary matches
        cells = [_plain(c).lower() for c in _wiki_cells(action)]
        keywords, arguments = cells[::2], len(cells[1::2])
        found = [
            a for a in actions
            if [k.lower() for k in a["keywords"]] == keywords and len(a["parameters"]) == arguments
        ]
    else:  # bare keywords
        wanted = " ".join(_plain(action).lower().split())
        found = [a for a in actions if " ".join(a["keywords"]).lower() == wanted]
    if not found:
        raise ToolError(
            f"{action!r} is not among the {len(actions)} defined actions of this session; see "
            "fitnesse_debug_inspect(kind='definedActions', filter=...)."
        )
    if len(found) > 1:
        raise ToolError(f"{action!r} is ambiguous: {[a['action'] for a in found]}. Pass the full call.")
    return found[0]


def _needed(value, name: str, action: str):
    """value, an argument action can't do without."""
    if value is None:
        raise ToolError(f"action='{action}' needs {name}.")
    return value


def _only_for(action: str, arguments: dict[str, tuple[object, tuple[str, ...]]]) -> None:
    """Refuse an argument given for an action that doesn't take it, before anything is saved."""
    for name, (value, actions) in arguments.items():
        if value is not None and action not in actions:
            raise ToolError(
                f"{name} does not go with action='{action}' (only with {' and '.join(actions)}). "
                "Nothing was changed.")


_DEFINED_ACTION_FORMAT = (
    "A defined action on a library page is a signature table with plain parameter names, e.g. "
    "|greet|name|, then body tables using @{name}, e.g. |show|@{name}|; no |define action| "
    "table. Put a ---- line between two definitions, or the second one is lost silently."
)


def _save_and_reload(page_path: str, action: str, form: dict, timeout_seconds: float | None, reload: bool) -> dict:
    """A debugTable save; with reload, the page's defined actions are loaded into the running
    session afterwards: by the fork itself (reload=true) where it can, else here via runWiki."""
    state = _debug_state()
    session_state = None
    if reload and state.session:
        form = {**form, "reload": True, "session": state.session}
        session_state = state  # the session cookie, without which the fork answers noSession
    result = _with_source(page_path, _without_html(
        _debug_post("debugTable", page_path, action, form, timeout_seconds=timeout_seconds, state=session_state)))
    if not reload:
        return result
    if "reloaded" not in result:  # older fork: reload here
        return {**result, "reload": _reload_defined_actions(page_path)}
    page = page_path.strip().lstrip(".")
    reload_result: dict = {"reloaded": bool(result.get("reloaded"))}
    actions = result.get("definedActions")
    if isinstance(actions, list):
        reload_result["definedActions"] = [
            _short_action(_json_defined_action(a)) for a in actions
            if isinstance(a, dict) and str(a.get("page", "")).lstrip(".") == page
        ]
    for key in ("error", "code"):
        if result.get(key):
            reload_result[key] = result[key]
    if isinstance(reload_result.get("error"), str):  # the fork names the field it sends; here it is 'tables'
        reload_result["error"] = reload_result["error"].replace("reloadHtml", "tables")
    if not reload_result["reloaded"] and isinstance(result.get("reloadHtml"), str):
        reload_result["tables"] = _tables_text(result["reloadHtml"])
    if isinstance(result.get("reloadCounts"), dict) and not reload_result["reloaded"]:
        reload_result["counts"] = result["reloadCounts"]
    if not reload_result["reloaded"]:
        reload_result["hint"] = (
            "The page is saved, but the session still runs the old version. Fix the definition and "
            f"save again, or run |define actions at|.{page}| with fitnesse_debug_run_wiki. "
            "A reload adds and replaces actions, it never removes one: after a wrong format start "
            "a new session. " + _DEFINED_ACTION_FORMAT)
    elif isinstance(actions, list) and not reload_result.get("definedActions"):
        reload_result["hint"] = (
            "Reloaded, but the page defined no action. " + _DEFINED_ACTION_FORMAT)
    rest = {k: v for k, v in result.items()
            if k not in {"reloaded", "definedActions", "error", "code", "reloadHtml", "reloadCounts", "keywords"}}
    return {**rest, "reload": reload_result}


def _with_source(page_path: str, result: dict) -> dict:
    """The page after a debugTable edit as 'source' (as fitnesse_table_source), for the next edit:
    from the answer where the fork sends it (mod #157), else read here. The page is already saved,
    so a failed read only adds a hint."""
    if isinstance(result.get("source"), dict):
        return result
    try:
        source = _debug_post("debugTable", page_path, "source", timeout_seconds=30.0)
    except ToolError as exc:
        return {**result, "hint": f"Saved, but reading the page again failed: {exc}. Read fitnesse_table_source before the next edit."}
    return {**result, "source": source}


def _without_html(result: dict) -> dict:
    """A debugTable answer with its rendered html condensed to tables text."""
    if isinstance(result, dict) and isinstance(result.get("html"), str):
        html = result["html"]
        result = {k: v for k, v in result.items() if k != "html"}
        result["tables"] = _tables_text(html)
    return result


def _reload_defined_actions(page: str) -> dict:
    """Run |define actions at|.page| in the session; never fails: the page is already saved."""
    page = page.strip().lstrip(".")
    state = _debug_state()
    if not state.session:
        return {"reloaded": False, "reason": "No debug session: saved only. A new session loads the new version."}
    try:
        with _document(state):
            data = _session_action(
                state, "runWiki", {"wiki": f"|define actions at|.{page}|"},
                timeout_seconds=DEBUG_TIMEOUT_SECONDS,
            )
            counts = data.get("counts") or {}
        actions = _session_defined_actions(state, DEBUG_TIMEOUT_SECONDS)
    except ToolError as exc:
        return {"reloaded": False, "reason": f"Saved, but reloading failed: {exc}. Run |define actions at|.{page}| with fitnesse_debug_run_wiki later."}
    result: dict = {
        "reloaded": not (counts.get("wrong") or counts.get("exceptions")),
        "definedActions": [_short_action(a) for a in actions if a["page"] == page],
    }
    if not result["reloaded"]:
        result["tables"] = _tables_text(data.get("html") or "")
    return result


_LOG_LINE_CHARS = 500  # the test process' command line alone (its classpath) has several KB


def _clock(ms, with_date: bool = False) -> str:
    """A time of the fork (epoch ms: log lines, startedAt) as this server's local time with its UTC
    offset, Z for UTC: 16:31:18.391Z, with_date 2026-11-09 16:31:18Z. Anything else as it came."""
    try:
        moment = datetime.fromtimestamp(int(ms) / 1000).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return "" if ms is None else str(ms)
    offset = moment.strftime("%z")
    zone = "Z" if offset in ("", "+0000", "-0000") else f"{offset[:3]}:{offset[3:]}"
    if with_date:
        return moment.strftime("%Y-%m-%d %H:%M:%S") + zone
    return f"{moment:%H:%M:%S}.{moment.microsecond // 1000:03d}{zone}"


def _readable_times(status: dict) -> dict:
    """status with startedAt, its own and its sessions', as local time (_clock)."""
    result = dict(status)
    if "startedAt" in result:
        result["startedAt"] = _clock(result["startedAt"], with_date=True)
    if isinstance(result.get("sessions"), list):
        result["sessions"] = [
            {**s, "startedAt": _clock(s["startedAt"], with_date=True)} if isinstance(s, dict) and "startedAt" in s else s
            for s in result["sessions"]
        ]
    return result


# The test process's log (debug-mode.md, Output): raw lines; an entry is a line with its
# continuation lines, as the debug UI groups them (continuedEntry in debugTest.js): same stream,
# at most 1 s apart, fewer than 2000 lines, no head of its own, and the line continues the entry:
# any line of a log4j entry up to its first stack frame (a logged REST request's headers and
# body), else a JUL message line, an indented line, "Caused by:", an exception's first line, or
# a line of an exception's message up to its first frame. Other lines after a stack trace, e.g. a
# fixture's output, are entries of their own. Heads: log4j's "[LEVEL] date time [thread] Logger -
# msg", or java.util.logging's first line "Nov 02, 2026 10:12:47 AM org.example.Class method"
# (its next line: "LEVEL: msg").
_LOG4J_HEAD = re.compile(r"^\[(TRACE|DEBUG|INFO|WARN|ERROR|FATAL) *\] \S+ \S+ \[[^\]]*\] (\S+) - ")
_JUL_HEAD = re.compile(r"^\S+ \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} \S+ ([\w$.]+) \S+$")
_JUL_LEVEL = re.compile(r"^(SEVERE|WARNING|INFO|CONFIG|FINEST|FINER|FINE): ")
_JUL_LEVELS = {"SEVERE": "ERROR", "WARNING": "WARN", "INFO": "INFO", "CONFIG": "DEBUG",
               "FINE": "DEBUG", "FINER": "TRACE", "FINEST": "TRACE"}
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")
_CONTINUED_LINE = re.compile(
    r"^(?:(?:SEVERE|WARNING|INFO|CONFIG|FINEST|FINER|FINE): |\s|Caused by: )|^[\w$.]+(?:Exception|Error)(?::|$)")
_EXCEPTION_LINE = re.compile(r"^Caused by: |^[\w$.]+(?:Exception|Error)(?::|$)")
_STACK_FRAME = re.compile(r"^\s+(?:at |\.\.\. \d+ )")
_ENTRY_MILLIS = 1000
_ENTRY_MAX_LINES = 2000  # the UI's ENTRY_LINES: a longer entry is output that doesn't stop
_ENTRY_LINES = 40  # lines shown per entry; a REST body or stack trace can run to hundreds


def _log_entries(lines) -> list[dict]:
    """Group raw log lines into entries: time, stream, level and logger (from the head, else
    None), lines, and first/last, the positions of its lines in the list."""
    entries: list[dict] = []
    last: dict[str, dict] = {}  # per stream, the entry a line may continue
    for n, line in enumerate(lines or []):
        if not isinstance(line, dict):
            continue
        text, stream, at = str(line.get("text", "")), str(line.get("stream", "")), line.get("time")
        head = _LOG4J_HEAD.match(text)
        jul = None if head else _JUL_HEAD.match(text)
        entry = last.get(stream)
        if (entry is not None and head is None and jul is None and isinstance(at, (int, float))
                and isinstance(entry["end"], (int, float)) and 0 <= at - entry["end"] <= _ENTRY_MILLIS
                and len(entry["lines"]) < _ENTRY_MAX_LINES
                and ((entry["log4j"] and not entry["framed"]) or entry["message"]
                     or _CONTINUED_LINE.match(text))):
            if entry["jul"] and len(entry["lines"]) == 1 and (level := _JUL_LEVEL.match(text)):
                entry["level"] = _JUL_LEVELS[level[1]]
            entry["lines"].append(text)
            entry["end"], entry["last"] = at, n
            _follow_exception(entry, text)
            continue
        entry = {"time": at, "end": at, "stream": stream, "lines": [text], "first": n, "last": n,
                 "level": head[1] if head else None, "jul": jul is not None,
                 "logger": head[2] if head else (jul[1].rsplit(".", 1)[-1] if jul else None),
                 "log4j": head is not None, "framed": False, "message": False}
        _follow_exception(entry, text)
        entries.append(entry)
        if stream in ("out", "err"):
            last[stream] = entry
    return entries


def _follow_exception(entry: dict, text: str) -> None:
    """An exception's first line opens its message, which ends at the first stack frame."""
    if _STACK_FRAME.match(text):
        entry["framed"], entry["message"] = True, False
    elif _EXCEPTION_LINE.match(text):
        entry["message"] = True


def _log_text(line: str) -> str:
    text = _mask_secrets(line)
    return f"{text[:_LOG_LINE_CHARS]}… ({len(text)} chars)" if len(text) > _LOG_LINE_CHARS else text


def _log_entry(entry: dict) -> str:
    """'time [stream] text', continuation lines below it; secrets masked, long lines cut."""
    lines = entry["lines"]
    shown = [_log_text(l) for l in lines[:_ENTRY_LINES]]
    if len(lines) > _ENTRY_LINES:
        shown.append(f"… ({len(lines) - _ENTRY_LINES} more lines)")
    return f"{_clock(entry['time'])} [{entry['stream']}] " + "\n".join(shown)


def _log_lines(lines) -> list[str]:
    return [_log_entry(e) for e in _log_entries(lines)]


def _entry_wanted(entry: dict, level: str | None, needle: str | None) -> bool:
    """level: entries below it are left out; 'info' lines (command, exit code, crashes) and
    lines without a level (counted as INFO) stay unless level is above INFO."""
    if level and entry["stream"] != "info":
        rank = LOG_LEVELS.index(entry["level"] or "INFO")
        if rank < LOG_LEVELS.index(level):
            return False
    return not needle or needle.lower() in "\n".join(entry["lines"]).lower()


def _log_lines_total(state: _DebugState, timeout_seconds: float) -> int | None:
    """Number of log lines so far: a log call from a line far ahead answers no lines, only 'next'."""
    try:
        return int(_session_action(state, "log", {"from": 999_999_999}, timeout_seconds=timeout_seconds).get("next"))
    except (ToolError, TypeError, ValueError):
        return None


def _log_from(total: int | None, unread: int, max_lines: int) -> int | None:
    """logFrom for end/abort. FitNesse sends every line from logFrom on (the test process' command
    line alone has ~40 KB), so start max_lines before the end; None leaves the log out."""
    if not max_lines:
        return None
    return unread if total is None else max(unread, total - max_lines)


def _read_log(
    state: _DebugState, max_entries: int, timeout_seconds: float | None,
    level: str | None = None, needle: str | None = None, newest: bool = False,
) -> dict:
    """The unread log as entries, oldest first; level/needle leave entries out (left_out). newest:
    the last max_entries of them, the older unread ones skipped. Moves log_next past what was read."""
    start = state.log_next
    data = _session_action(state, "log", {"from": start}, timeout_seconds=timeout_seconds)
    lines = data.get("lines") or []
    dropped = data.get("dropped") or 0
    base = start + dropped  # the first returned line's number
    end = data.get("next", base + len(lines))
    entries = _log_entries(lines)
    kept = [e for e in entries if _entry_wanted(e, level, needle)]
    result: dict = {"dropped": dropped}
    if newest:
        shown = kept[-max_entries:]
        if len(kept) > len(shown):
            result["skipped"] = len(kept) - len(shown)
        result["more"] = False
    else:
        shown = kept[:max_entries]
        result["more"] = len(kept) > max_entries
        if result["more"]:  # the next call goes on after the last entry shown
            end = base + shown[-1]["last"] + 1
    state.log_next = end
    left_out = sum(1 for e in entries if e not in kept and base + e["last"] < end)
    if left_out:
        result["left_out"] = left_out
    return {"lines": [_log_entry(e) for e in shown], **result}


_STOP_LOG_WAIT = 0.5  # seconds: browser events (BiDi) reach the log just after the table's answer
_STOP_LOG_ENTRIES = 15
_STOP_LOG_SLACK = 1000  # ms the two clocks may differ


def _stop_log(state: _DebugState, timeout_seconds: float | None) -> dict | None:
    """The warnings, errors and browser console entries written since the failing table began,
    the last _STOP_LOG_ENTRIES of them; None if there are none or the log can't be read."""
    if state.table_started is None:
        return None
    time.sleep(_STOP_LOG_WAIT)
    try:  # max (mod #162): only the end of the log; older forks send all of it
        data = _session_action(state, "log", {"from": 0, "max": 400}, timeout_seconds=timeout_seconds or 30.0)
    except ToolError as exc:
        logger.info("log of the failing table not read: %s", exc)
        return None
    since = state.table_started - _STOP_LOG_SLACK
    lines = [l for l in data.get("lines") or []
             if isinstance(l, dict) and isinstance(l.get("time"), (int, float)) and l["time"] >= since]
    found = [e for e in _log_entries(lines)
             if e["logger"] == "BROWSER_CONSOLE" or LOG_LEVELS.index(e["level"] or "INFO") >= LOG_LEVELS.index("WARN")
             or (e["stream"] == "info" and e["lines"] and e["lines"][0] and "exit code" not in e["lines"][0].lower())]
    if not found:
        return None
    result: dict = {"lines": [_log_entry(e) for e in found[-_STOP_LOG_ENTRIES:]]}
    if len(found) > _STOP_LOG_ENTRIES:
        result["omitted"] = len(found) - _STOP_LOG_ENTRIES
    return result


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local(url: str) -> bool:
    return (parse.urlsplit(url).hostname or "") in _LOCAL_HOSTS


def _grid_root(hub: str, instance: str) -> str:
    """The Selenium Grid's address from a hub URL: without /wd/hub and the trailing /. A hub on
    localhost is the test process's view: a KDTA instance serves its grid at <instance>/grid/hub."""
    root = hub.strip().rstrip("/").removesuffix("/wd/hub")
    if root and _is_local(root) and not _is_local(instance):
        return instance.rstrip("/") + "/grid/hub"
    return root


def _browser_view(template: str | None, hub: str, session: str, instance: str) -> str | None:
    """Where a person watches one browser: debugTest.liveViewUrl with its {session}/{hub}
    placeholders filled in (the fork sends it raw), else the Grid UI's page of the session."""
    if not session:
        return None
    root = _grid_root(hub, instance)
    if template:
        return template.replace("{session}", session).replace("{hub}", root)
    return f"{root}/ui/#/session/{session}" if root else None


def _live_view(data: dict) -> dict:
    """An answer of action=liveView (also the liveView extra of a run): the fields of mod #163,
    else the cells of its table (liveView | hub | session | browsers JSON)."""
    cells = ((_query_rows(data.get("html") or "") or [[]])[0] + [""] * 4)[:4]
    hub = data.get("hub") if isinstance(data.get("hub"), str) else cells[1]
    session = data.get("seleniumSession") if isinstance(data.get("seleniumSession"), str) else cells[2]
    browsers = data.get("browsers")
    if not isinstance(browsers, list):
        try:
            browsers = json.loads(cells[3]) if cells[3] else []
        except json.JSONDecodeError:
            browsers = []
    template = data.get("liveViewUrl") if isinstance(data.get("liveViewUrl"), str) else None
    instance = _base_url()
    listed = [
        {**b, "live_view_url": _browser_view(template, str(b.get("hub") or hub), str(b.get("session") or ""), instance)}
        for b in browsers if isinstance(b, dict)
    ]
    result = {
        "hub": hub,
        "selenium_session": session,
        "live_view_url": _browser_view(template, hub, session, instance),
        "browsers": listed,
        "live_view_only": data.get("liveViewOnly"),
    }
    if not session:
        result["hint"] = "No remote browser in this session yet: a table opens one (usually the SetUp)."
    elif _is_local(result["live_view_url"] or ""):
        # only when the link itself is local: a configured liveViewUrl usually names the real host
        result["note"] = ("The link uses the hub's address as the test process sees it (" + hub + "): "
                          "from elsewhere it may need the host's real name instead.")
    return _without_secrets(result)


def _variables(html: str, include_system: bool = False) -> list[dict]:
    records = _query_records(html, "dynamicVariables")
    for record in records:
        record["system"] = record.get("system", "").lower() == "true"
    return records if include_system else [r for r in records if not r["system"]]


def _run_form(form: dict, variables: bool, live_view: bool) -> dict:
    """Ask for the extras of a run (mod v1.5.0: withDynamicVariables, v1.6.0: withLiveView)."""
    return {**form, "withDynamicVariables": True if variables else None, "withLiveView": True if live_view else None}


def _with_extras(state, data: dict, result: dict, variables: bool, live_view: bool, timeout_seconds) -> dict:
    """Add the variables and the live view after a run: from the run's answer, or, where the fork
    left them out (older fork, a failing query), from the separate actions."""
    if variables:
        html = data.get("dynamicVariables")
        if not isinstance(html, str):
            html = _session_action(state, "dynamicVariables", timeout_seconds=timeout_seconds).get("html") or ""
        result["dynamicVariables"] = _variables(html)
    if live_view:
        view = data.get("liveView")
        if not isinstance(view, dict):
            view = _session_action(state, "liveView", timeout_seconds=timeout_seconds)
        result["liveView"] = _live_view(view)
    return result


def _suite_frame(state: _DebugState, timeout_seconds: float | None) -> list:
    """Single-page session: remember the page's SuiteSetUp/SuiteTearDown from suitePages (they are
    not in pageHtml) and return the pages. Older forks without suitePages outside a suite: the page
    alone."""
    alone = [{"path": state.page, "kind": "test", "runnable": True}]
    try:
        pages = _session_action(state, "suitePages", timeout_seconds=timeout_seconds).get("pages")
    except ToolError as exc:
        logger.info("suitePages for a single page: %s", exc)
        return alone
    if not isinstance(pages, list) or not pages:
        return alone
    for entry in pages:
        if not isinstance(entry, dict) or entry.get("runnable") is False:
            continue
        path = str(entry.get("path") or "").lstrip(".")
        if entry.get("kind") == "suiteSetUp":
            state.suite_setup = path or None
        elif entry.get("kind") == "suiteTearDown":
            state.suite_teardown = path or None
    return pages


def _page_flags(page_path: str) -> dict | None:
    """The page's properties (Test, Suite, …); None when they can't be read."""
    result = _request("GET", page_path, params={"responder": "properties", "format": "json"}, timeout_seconds=30.0)
    flags = result.get("body") if result.get("ok") else None
    return flags if isinstance(flags, dict) else None


def _check_exists(page_path: str) -> None:
    """For a page whose properties say neither test nor suite: FitNesse answers properties of a
    missing page with 200 and every flag false, so ask pageData (404 there) and say so."""
    if _request("GET", page_path, params={"responder": "pageData"}, timeout_seconds=30.0).get("status") == 404:
        raise ToolError(
            f"{page_path} does not exist on this FitNesse. Check the path: fitnesse_search finds "
            "pages by name."
        )


def _is_suite(page_path: str) -> tuple[bool, str | None]:
    """Whether to start page_path as a suite, from its Suite/Test properties, and a note for the
    result. Properties that can't be read: a test page (the start then says what is wrong)."""
    flags = _page_flags(page_path)
    if flags is None:
        return False, None
    suite, test = bool(flags.get("Suite")), bool(flags.get("Test"))
    if suite and test:
        return True, "The page is a suite and a test page; started as a suite. suite=false runs only its own tables."
    if not suite and not test:
        _check_exists(page_path)
        raise ToolError(
            f"{page_path} is neither a test nor a suite page (its properties). Start a test or suite "
            "page, or pass suite=true or suite=false to start it anyway."
        )
    return suite, None


def _check_runnable(state: _DebugState, page: str) -> None:
    """run_page on a page outside the session's pages: refuse one that is neither a test nor a
    suite page, as start does (a Static page would run as a test without a word). Properties that
    can't be read: the run says what is wrong."""
    if page in (state.page, state.current_page) or page in state.pages:
        return
    flags = _page_flags(page)
    if flags is not None and not flags.get("Test") and not flags.get("Suite"):
        _check_exists(page)
        raise ToolError(
            f"{page} is neither a test nor a suite page (its properties) and not one of this "
            "session's pages (fitnesse_debug_start's 'pages'). Run a test page, or try its tables "
            "with fitnesse_debug_run_wiki."
        )


def _session_pages(pages) -> set[str]:
    """The paths of fitnesse_debug_start's 'pages' (suitePages)."""
    if not isinstance(pages, list):
        return set()
    return {key for p in pages if isinstance(p, dict) and (key := _page_key(str(p.get("path") or "")))}


def _continue_page(state: _DebugState, page: str, stop_on_failure: bool, timeout_seconds: float | None) -> dict:
    """Run the current page (page) on from next_table to its end; with stop_on_failure one table
    at a time up to the first failing one (stopped_at, next_table), else in one go."""
    html_page = _page_html(state, page, timeout_seconds)
    parsed = _parse_tables(html_page)
    parts = [html_page[t["start"]:t["end"]] for t in parsed]
    state.table_count = len(parts)
    chosen = list(range(state.next_table, len(parts)))
    if not chosen:
        state.next_table = len(parts)
        return {"page": page, "page_done": True}
    if stop_on_failure:
        data, ran, failed = _run_tables_stepwise(state, parts, chosen, timeout_seconds)
    else:
        html = html_page if chosen[0] == 0 else "\n".join(parts[i] for i in chosen)
        data = _session_action(state, "runTable", {"html": html}, timeout_seconds=timeout_seconds)
        ran, failed = chosen, None
    state.next_table = ran[-1] + 1
    _seen_page(state, parsed, ran)
    result = _document_result(state, data, page, include_html=False, numbers=ran)
    if failed is None:
        result["page_done"] = True
    else:
        result["stopped_at"] = _stop_details(state, html_page, parsed, page, failed, timeout_seconds)
        result["next_table"] = state.next_table
        result["_images"] = result.get("_images", []) + _failure_screenshot(data.get("html") or "")
    return result


def _run_suite_setup(
    state: _DebugState, page: str, stop_on_failure: bool, timeout_seconds: float | None,
    restart: bool = False,
) -> dict | None:
    """Single-page session: run the SuiteSetUp (if any) before the page's test, as ?test does,
    then begin page's test again. Like a page, it stops at its first failing table: then the
    page's test has not begun (suite_setup_pending), and the next run of the page continues the
    SuiteSetUp first; restart runs it again from its first table instead. Returns the
    SuiteSetUp's result when this call ran some of it.
    The first page run starts the frame: from then on end runs the SuiteTearDown (if any), with
    or without a SuiteSetUp."""
    if state.suite:
        return None
    restarted = False
    if not state.frame_started:
        state.frame_started = True
        if not state.suite_setup:
            return None
        _next_page(state, state.suite_setup, timeout_seconds)
        state.suite_setup_pending = True
    elif restart and state.suite_setup_pending and page != state.suite_setup:
        _next_page(state, state.suite_setup, timeout_seconds)  # its test begins afresh, as for a page
        restarted = True
    if not state.suite_setup_pending or page == state.suite_setup:
        return None  # run_page on the SuiteSetUp itself works on it as on any page
    result = _continue_page(state, state.suite_setup, stop_on_failure, timeout_seconds)
    if restarted:
        result["restarted"] = True
    if "stopped_at" not in result:
        state.suite_setup_pending = False
        _next_page(state, page, timeout_seconds)
    return result


def _has_suite_teardown(state: _DebugState) -> bool:
    """Single-page session with a SuiteTearDown, after a page run started the frame (see
    _run_suite_setup): end runs the SuiteTearDown."""
    return not state.suite and bool(state.suite_teardown) and state.frame_started


def _run_suite_teardown(state: _DebugState, result: dict, timeout_seconds: float | None) -> bool:
    """Before end: the page's TearDown (_end_test), its storytest's end (nextPage) and the
    SuiteTearDown, if _has_suite_teardown. Whether it ran."""
    if not _has_suite_teardown(state):
        return False
    try:
        result["teardown"] = _next_page(state, state.suite_teardown, timeout_seconds)
        result["teardown"]["page"] = result["teardown"].pop("teardown_of")
        html = _page_html(state, state.suite_teardown, timeout_seconds)
        data = _session_action(state, "runTable", {"html": html}, timeout_seconds=timeout_seconds)
        result["suite_teardown"] = _document_result(state, data, state.suite_teardown, include_html=False)
    except ToolError as exc:
        result["suite_teardown_error"] = str(exc)
    return True


def _run_document(
    state: _DebugState, page: str | None, restart: bool, timeout_seconds: float | None, run,
    skip_teardown: bool = False,
) -> dict:
    """Begin page's storytest if needed (nextPage, after the current page's TearDown), then run();
    report how the previous test ended."""
    teardown = None
    if (page and page != state.current_page) or restart:
        try:
            teardown = _next_page(state, page, timeout_seconds, skip_teardown)
        except _TearDownFailed as exc:
            raise ToolError(f"{exc} To go on without it, call this again with skip_teardown=true.") from exc
    result = run()
    if teardown is not None:
        result["previous_page_teardown"] = teardown
    return result


if DEBUG_TOOLSET:

    @mcp.tool(
        name="fitnesse_debug_start",
        description=(
            "Starts a debug session on a KDTA FitNesse: a test process that stays alive between "
            "calls, so tables run one call at a time and the state (open browsers, objects in "
            "scope, variables) carries over. Use it to try out steps, build a new test or step "
            "through a failing one; for a plain run use fitnesse_run_test. Nothing is written to "
            "the test history.\n"
            "Start on the test or suite page you want to work on: it decides which fixtures and "
            "variables there are, and its type the kind of session. Then: "
            "fitnesse_debug_run_page(setup_only=true), since defined actions, the browser and often "
            "fixtures only exist once the SetUp has run -> fitnesse_debug_inspect -> "
            "fitnesse_debug_run_wiki or fitnesse_debug_run_page, repeatedly -> fitnesse_debug_end, "
            "always: the session holds a server slot and browsers until then.\n"
            "Result: suite (true/false) and pages in run order (path, kind test/suiteSetUp/"
            "suiteTearDown, runnable). Suite session: call fitnesse_debug_run_page for each page "
            "with runnable=true, in that order. Test page: its SuiteSetUp and SuiteTearDown run by "
            "themselves, with the first fitnesse_debug_run_page and with fitnesse_debug_end.\n"
            "Fails if a session is already running on this page (maybe a person's, in the browser): "
            + ("continue it with fitnesse_debug_status(adopt=true, session=...) or end it. "
               if ALLOW_ADOPT else
               "use another page or wait; this server does not take over sessions it did not start. ")
            + "This server holds one session at a time: end it before starting another."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_start(
        page_path: Annotated[str, Field(description="Dotted path of the test or suite page, e.g. 'FrontPage.Project.MyTest'.")],
        suite: Annotated[bool | None, Field(description="Leave out: the page's type decides (suite page: suite session, test page: that page). true/false only to override it.")] = None,
        replace: Annotated[bool, Field(description="Only when told to: kill a running session of this account on this page (older forks: anywhere; maybe a person's) without tear down.")] = False,
        suite_filter: Annotated[str | None, Field(description="Suites only: only tests matching any of these comma-separated tags.")] = None,
        run_tests_matching_all_tags: Annotated[str | None, Field(description="Suites only: only tests matching all of these comma-separated tags.")] = None,
        exclude_suite_filter: Annotated[str | None, Field(description="Suites only: leave out tests with any of these comma-separated tags.")] = None,
        first_test: Annotated[str | None, Field(description="Suites only: start the list at this test page.")] = None,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """properties (page type), status check, start and suitePages via responder=debugTest."""
        state = _debug_state()
        note = None
        if suite is None:
            suite, note = _is_suite(page_path)
        with _document(state):
            # without session: a stale id would answer 409 instead of describing the running session
            status = _status(page_path, state, 30.0, with_session=False)
            sessions = _sessions(status)
            if sessions is not None:  # mod #159: one session per page and account
                _check_start(page_path, sessions, state, replace)
            elif status.get("running") and not replace:
                owner = "this MCP server" if state.session and str(status.get("session")) == state.session else "someone using the same account"
                raise ToolError(
                    f"A debug session started by {owner} is already running (page "
                    f"{status.get('page')}, current page {status.get('currentPage')}, started "
                    f"{_clock(status.get('startedAt'), with_date=True)}); this FitNesse allows one per account. "
                    + ("Continue it with fitnesse_debug_status(adopt=true), end it with fitnesse_debug_end, "
                       "or pass replace=true to kill it without tear down."
                       if owner == "this MCP server" else _foreign_session_advice(None))
                )
            state.reset()
            state.steps, state.step_count = [], 0
            data = _debug_post(
                "debugTest", page_path, "start", {"suite": True if suite else None},
                timeout_seconds=timeout_seconds, state=state,
            )
            state.session = str(data.get("session"))
            state.page = _page_key(data.get("page")) or _page_key(page_path)
            state.current_page = state.page
            state.suite = suite
        data["suite"] = suite
        if note:
            data["note"] = note
        if not suite:
            data["pages"] = _suite_frame(state, 30.0)
            state.pages = _session_pages(data["pages"])
            if state.suite_setup:
                data["suite_setup"] = state.suite_setup
            if state.suite_teardown:
                data["suite_teardown"] = state.suite_teardown
            if suite_filter or run_tests_matching_all_tags or exclude_suite_filter or first_test:
                data["filters_ignored"] = "suite_filter, run_tests_matching_all_tags, exclude_suite_filter and first_test only apply to suites."
        if suite:
            try:
                pages = _session_action(
                    state, "suitePages",
                    {
                        "suiteFilter": suite_filter,
                        "runTestsMatchingAllTags": run_tests_matching_all_tags,
                        "excludeSuiteFilter": exclude_suite_filter,
                        "firstTest": first_test,
                    },
                )
                data["pages"] = pages.get("pages")
                state.pages = _session_pages(data["pages"])
            except ToolError as exc:
                data["pages_error"] = str(exc)
        return data

    _SKIP_TEARDOWN = Annotated[bool, Field(description=(
        "When this call ends the current page's test: leave out its TearDown tables that have not "
        "run yet (normally run first), e.g. to look at the data a stop left behind."))]

    _STATUS_TOOL = dict(
        name="fitnesse_debug_status",
        description=(
            "Shows the debug session and the new entries of the test process's log since the last "
            "call. Answers at once, also while another call is still running a long table: use it "
            "to watch progress.\n"
            "Result: running, page (where the session started), currentPage, suite, busy, "
            "idleSecondsLeft (the session ends itself after this idle time), "
            "known_to_this_server, sessions (all running sessions of the account) and log.lines, "
            "one log entry each ('time [out|err|info] text'; values of secrets are masked as "
            "***). log_level, log_filter and log_newest narrow the log; the entries they leave out "
            "(left_out, skipped) count as read.\n"
            "busy=true: the process is still working on a call that timed out. Call this again "
            "until it is false, or fitnesse_debug_end(abort=true). known_to_this_server=false with "
            "running=true: someone else's session, or one from before a restart; the other debug "
            "tools refuse to touch it"
            + (" until you call this with adopt=true (with several sessions: plus session=<id> from "
               "'sessions'). Only adopt a session you are meant to continue." if ALLOW_ADOPT else
               ", and this server does not take it over: leave it alone, it ends itself when idle.")
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": True},
    )
    _STATUS_PAGE = Annotated[str | None, Field(description="Rarely needed: page used for the permission check; defaults to the session's page, else FrontPage.")]
    _STATUS_LOG = Annotated[int, Field(description="Maximum number of new log entries to return; the rest follow on the next call. 0 for none.", ge=0)]
    _STATUS_LEVEL = Annotated[Literal[LOG_LEVELS] | None, Field(description="Only log entries of this level and above, e.g. 'WARN' (lines without a level count as INFO; 'info' lines stay).")]
    _STATUS_FILTER = Annotated[str | None, Field(description="Only log entries containing this text (case-insensitive), e.g. a logger such as 'BROWSER_CONSOLE', 'BROWSER_NETWORK' or 'FITLIBRARYWEB', or a word of the message.")]
    _STATUS_NEWEST = Annotated[bool, Field(description="The newest max_log_lines entries instead of the oldest unread ones, e.g. after a long run; the older unread ones are skipped.")]

    def _debug_status(
        adopt: bool, session: str | None, page_path: str | None, max_log_lines: int, timeout_seconds: float,
        log_level: str | None = None, log_filter: str | None = None, log_newest: bool = False,
    ) -> dict:
        """status (and log) via responder=debugTest."""
        state = _debug_state()
        status = _status(page_path or state.page or "FrontPage", state, timeout_seconds, with_session=False)
        running = bool(status.get("running"))
        sessions = _sessions(status)
        if sessions is not None:  # mod #159: this server's session may run on another page than asked
            known = bool(state.session) and any(s["session"] == state.session for s in sessions)
        else:
            known = running and bool(state.session) and str(status.get("session")) == state.session
        if adopt and not known:
            chosen = _session_to_adopt(status, sessions, session)
            if chosen is not None:
                state.reset()
                state.session = str(chosen.get("session"))
                state.page = _page_key(chosen.get("page"))
                state.current_page = _page_key(chosen.get("currentPage")) or state.page
                state.suite = bool(chosen.get("suite"))
                known = True
        lost = None
        if not known and state.session:
            lost = _lost_session(state, timeout_seconds)
            state.reset()
        result = {**_readable_times(status), "known_to_this_server": known}
        if lost:
            result["lost_session"] = lost
        if known and state.table_count is not None:  # where run_page continues the current page
            result["next_table"] = state.next_table
        if known and max_log_lines:
            result["log"] = _read_log(state, max_log_lines, timeout_seconds, log_level, log_filter, log_newest)
        return result

    if ALLOW_ADOPT:

        @mcp.tool(**_STATUS_TOOL)
        def fitnesse_debug_status(
            adopt: Annotated[bool, Field(description="Continue the running session of this account with this server's debug tools.")] = False,
            session: Annotated[str | None, Field(description="With adopt, when several sessions run: the id of the one to continue, from 'sessions'.")] = None,
            page_path: _STATUS_PAGE = None,
            max_log_lines: _STATUS_LOG = 50,
            log_level: _STATUS_LEVEL = None,
            log_filter: _STATUS_FILTER = None,
            log_newest: _STATUS_NEWEST = False,
            timeout_seconds: float = _make_timeout(30.0),
        ) -> dict:
            """status (and log) via responder=debugTest; adopt takes over a running session."""
            return _debug_status(adopt, session, page_path, max_log_lines, timeout_seconds,
                                 log_level, log_filter, log_newest)

    else:

        @mcp.tool(**_STATUS_TOOL)
        def fitnesse_debug_status(
            page_path: _STATUS_PAGE = None,
            max_log_lines: _STATUS_LOG = 50,
            log_level: _STATUS_LEVEL = None,
            log_filter: _STATUS_FILTER = None,
            log_newest: _STATUS_NEWEST = False,
            timeout_seconds: float = _make_timeout(30.0),
        ) -> dict:
            """status (and log) via responder=debugTest."""
            return _debug_status(False, None, page_path, max_log_lines, timeout_seconds,
                                 log_level, log_filter, log_newest)

    @mcp.tool(
        name="fitnesse_debug_run_wiki",
        description=(
            "Runs wiki tables in the debug session without saving anything, e.g. '|with browser|'. "
            "Variables of the current page such as ${BASE_URL} are resolved. The page's SetUp and "
            "TearDown are not added: run the SetUp first with "
            "fitnesse_debug_run_page(setup_only=true). Take the keywords' exact wording from "
            "fitnesse_debug_inspect(kind='keywords'). All tables of a call run before the result "
            "comes back, which can take minutes.\n"
            "Result: tables, each as text with one row per line. A cell's outcome is prefixed: "
            "[pass], [fail] ('<value> expected ⏎ <value> actual'), [error] (an exception), "
            "[ignore] (not run); cells without prefix are input. Below a table come its folding "
            "areas ('after: <title>'), links and images; screenshots are attached as images (at "
            f"most {DEBUG_MAX_IMAGES} per call). counts are this call's, page_totals those since "
            "the page's test began. A call whose tables all pass is noted as a step (step; see "
            "fitnesse_debug_steps).\n"
            "A [pass] on a click only means the call returned: check its effect (URL, a text, an "
            "element's visibility) before the next step. Every table with |with browser| starts its "
            "own browser, which stays in the grid until fitnesse_debug_end: start it once in the "
            "SetUp with |add global|with browser|."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_run_wiki(
        wiki: Annotated[str, Field(description="Wiki markup: one or more tables of '|cell|cell|' rows, tables separated by an empty line.")],
        restart: Annotated[bool, Field(description="First end the running test (tear down) and begin it afresh, for a clean state.")] = False,
        skip_teardown: _SKIP_TEARDOWN = False,
        record: Annotated[bool, Field(description="Note the wiki as a step when all its tables pass (step in the result; fitnesse_debug_steps lists them for saving). false for tables that only look around and don't belong in the test.")] = True,
        include_html: Annotated[bool, Field(description="Also return the raw result HTML (large; normally not needed).")] = False,
        with_variables: Annotated[bool, Field(description="Also return the dynamic variables after the run (as fitnesse_debug_inspect(kind='dynamicVariables')), in the same call.")] = False,
        with_live_view: Annotated[bool, Field(description="Also return the live view after the run (as fitnesse_debug_inspect(kind='liveView')), in the same call.")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """(nextPage +) runWiki via responder=debugTest."""
        state = _require_session()
        def run() -> dict:
            data = _session_action(state, "runWiki", _run_form({"wiki": wiki}, with_variables, with_live_view),
                                   timeout_seconds=timeout_seconds)
            result = _with_setup_hint(state, _document_result(state, data, state.current_page, include_html), timeout_seconds)
            if record and _all_passed(data):
                state.step_count += 1
                state.steps.append({"step": state.step_count, "page": state.current_page, "wiki": wiki.strip("\n") + "\n"})
                result["step"] = state.step_count
            return _with_extras(state, data, result, with_variables, with_live_view, timeout_seconds)

        with _document(state):
            return _with_images(_run_document(state, None, restart, timeout_seconds, run, skip_teardown))

    @mcp.tool(
        name="fitnesse_debug_steps",
        description=(
            "Lists the steps noted while building a test: every fitnesse_debug_run_wiki call whose "
            "tables all passed (unless run with record=false), in order: step, page, wiki. 'wiki' "
            "joins them, ready to save on the page with fitnesse_table_edit(action='insert'). drop "
            "forgets single steps (tries that don't belong in the test), clear=true all of them, "
            "e.g. after saving. They outlast fitnesse_debug_end; a new fitnesse_debug_start clears "
            "them."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def fitnesse_debug_steps(
        drop: Annotated[list[int] | None, Field(description="Numbers of steps to forget. If one of them was not noted, nothing is dropped.")] = None,
        clear: Annotated[bool, Field(description="Forget all steps (after returning them).")] = False,
    ) -> dict:
        """The steps noted in this server's memory; no request to FitNesse."""
        state = _debug_state()  # no session lock: it is held while a table runs
        if drop:
            noted = [s["step"] for s in state.steps]
            unknown = sorted(set(drop) - set(noted))
            if unknown:
                raise ToolError(
                    f"No step {', '.join(map(str, unknown))}: the noted steps are "
                    f"{', '.join(map(str, noted)) or 'none'}. Nothing was dropped; call this without "
                    "drop to see them."
                )
            state.steps = [s for s in state.steps if s["step"] not in set(drop)]
        steps = list(state.steps)
        if clear:
            state.steps = []
        result: dict = {"steps": steps, "wiki": "\n".join(s["wiki"] for s in steps)}
        if not steps:
            result["hint"] = "No steps noted: fitnesse_debug_run_wiki notes the calls whose tables all pass."
        elif clear:
            result["cleared"] = True
        return result

    @mcp.tool(
        name="fitnesse_debug_page_tables",
        description=(
            "Lists the tables a run of the page executes, in order, with those of its SetUp, "
            "TearDown and included pages: index, rows, first_row, source_page and source_index. "
            "The indices are for fitnesse_debug_run_page(tables=[...]); setup_tables and "
            "teardown_tables say which of them are the SetUp's and the TearDown's. To edit a table, "
            "pass its source_page and source_index to fitnesse_table_source and fitnesse_table_edit, "
            "never its index: those count only a page's own tables. included=true: the table is on "
            "an included page (e.g. the SetUp), and editing it changes every page that includes "
            "it. Needs a debug session; without page_path the page being tested."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_debug_page_tables(
        page_path: DebugPath = None,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """pageHtml via responder=debugTest, split into top-level tables."""
        state = _require_session()
        page = _page_key(page_path) or state.current_page or state.page
        tables = []
        parsed = _parse_tables(_page_html(state, page, timeout_seconds))
        for i, (table, (source_page, source_index)) in enumerate(zip(parsed, _table_sources(parsed, page))):
            first = " | ".join(_squash(c["text"], 200) for c in (table["rows"][0] if table["rows"] else []))
            entry = {"index": i, "rows": len(table["rows"]), "first_row": first,
                     "source_page": source_page, "source_index": source_index}
            if table["include"]:
                entry["included"] = True
            tables.append(entry)
        return {"page": page, "setup_tables": _setup_indices(parsed), "teardown_tables": _teardown_indices(parsed),
                "tables": tables}

    @mcp.tool(
        name="fitnesse_debug_read_link",
        description=(
            "Reads a file a debug result links to: the screenshot ('Screen', "
            "/files/screenCaptures/...) or the page source at the failure ('HTML', "
            "/files/htmlSource/...) that KDTA links from a failing table. An image comes back as an "
            "image. HTML and text come without scripts, styles and comments: with filter only the "
            "places that contain it, to check a locator against the page as it was (an id, a "
            "class, a visible text); without filter the beginning and the length. Only links "
            "into this FitNesse's files section; needs no session."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_debug_read_link(
        url: Annotated[str, Field(description="The /files/... link from a debug result, e.g. '/files/htmlSource/2026/11/07/03_06_12_683.html'.")],
        filter: Annotated[str | None, Field(description="HTML/text only: return only the places that contain this text (case-insensitive), e.g. 'v-detail-model-value'.")] = None,
        context_chars: Annotated[int, Field(description="With filter: characters shown before and after each place.", ge=0, le=5000)] = 300,
        max_matches: Annotated[int, Field(description="With filter: maximum number of places returned.", ge=1)] = 20,
        max_chars: Annotated[int, Field(description="Without filter: maximum number of characters returned from the start.", ge=500)] = 20000,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict | ToolResult:
        """GET /files/... (Basic Auth as for every request)."""
        path = _files_link(url)
        got = _request_bytes("GET", path, timeout_seconds=timeout_seconds)
        if not got.get("ok"):
            raise ToolError(f"Could not read {path}: HTTP {got.get('status')} {got.get('error') or ''}".strip()
                            + ". Files from older runs may have been cleaned up.")
        body: bytes = got["body"]
        fmt = _image_format(path) or (got.get("content_type", "").split("/", 1)[1].split(";")[0]
                                      if got.get("content_type", "").startswith("image/") else None)
        if fmt:
            if got.get("truncated"):
                raise ToolError(f"{path} is larger than FITNESSE_FILES_MAX_TRANSFER_BYTES.")
            meta = {"link": path, "format": fmt, "bytes": len(body)}
            return ToolResult(
                content=[TextContent(type="text", text=json.dumps(meta)), Image(data=body, format=fmt).to_image_content()],
                structured_content=meta,
            )
        text = _link_text(body, got.get("content_type", ""))
        result: dict = {"link": path, "length": len(text)}
        if got.get("truncated"):
            result["truncated"] = True  # only the start of the file was read
        if filter:
            places, snippets = _snippets(text, filter, context_chars, max_matches)
            result.update({"filter": filter, "places": places, "snippets": snippets})
            if not places:
                result["hint"] = "Not found: the element was not on the page then, or try a shorter or other text."
            return result
        result["text"] = text[:max_chars]
        if len(text) > max_chars:
            result["cut"] = len(text) - max_chars
            result["hint"] = "Pass filter (an id, a class, a text) to see the places you need."
        return result

    @mcp.tool(
        name="fitnesse_debug_run_page",
        description=(
            "Runs the tables of a test page in the debug session as a normal run would send them "
            "(SetUp, TearDown and included pages included) and stops after the first table with a "
            "[fail] or [error] cell, like a debugger: the rest of the page, its TearDown included, "
            "has not run, and the browser and state are as the failure left them.\n"
            "On a stop the result has stopped_at (index, source_page, source_index, text_before, "
            "wiki, log), next_table and, when an element failed, its screenshot as an image; its "
            "'HTML' link, the page source then, is read with fitnesse_debug_read_link(url, "
            "filter=...). Not for |check|title|. "
            "Try corrected versions of stopped_at.wiki with fitnesse_debug_run_wiki until one "
            "passes and save that one with fitnesse_table_edit(source_page, source_index, "
            "original=stopped_at.wiki), or "
            "report that there is no fix (e.g. the application is wrong). "
            "Then call this again without arguments: it continues at next_table. page_done=true: "
            "all tables ran. Result tables as in fitnesse_debug_run_wiki.\n"
            "setup_only=true runs just the SetUp: do that right after fitnesse_debug_start; a later "
            "run of the page then leaves the SetUp out. tables=[...] runs only those indices of "
            "fitnesse_debug_page_tables and does not move next_table. On a single test page the "
            "first call runs its SuiteSetUp before (suite_setup), which stops at a failure the "
            "same way.\n"
            "page_path other than the current page (a test page, or one of fitnesse_debug_start's "
            "pages) first ends the current page's test as a normal run would, with its TearDown "
            "tables that have not run yet (previous_page_teardown), then begins the new one. In a "
            "suite session call this for each runnable page in order, moving on once page_done is "
            "true."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_run_page(
        page_path: DebugPath = None,
        tables: Annotated[list[int] | None, Field(description="Indices from fitnesse_debug_page_tables to run, in this order (the result numbers the tables the same way); omit to run (or continue) the whole page. Including all setup_tables counts as running the SetUp.")] = None,
        restart: Annotated[bool, Field(description="Begin the page's test afresh even if it is already the current page. While a single page's SuiteSetUp is stopped: run the SuiteSetUp again from its first table.")] = False,
        skip_teardown: _SKIP_TEARDOWN = False,
        setup_only: Annotated[bool, Field(description="Run only the SetUp tables (setup_tables of fitnesse_debug_page_tables). Not together with tables.")] = False,
        from_table: Annotated[int | None, Field(description="Continue the page at this index instead of next_table, e.g. after tables before it were inserted or deleted. Not together with tables or setup_only.", ge=0)] = None,
        stop_on_failure: Annotated[bool, Field(description="Stop after the first table with a [fail] or [error] cell. false runs all chosen tables in one go.")] = True,
        only_failures: Annotated[bool, Field(description="Return only the tables with a [fail] or [error] cell (counts as usual; tables_without_failures says how many were left out). For long runs with stop_on_failure=false.")] = False,
        include_html: Annotated[bool, Field(description="Also return the raw result HTML (large; normally not needed).")] = False,
        with_variables: Annotated[bool, Field(description="Also return the dynamic variables after the run (as fitnesse_debug_inspect(kind='dynamicVariables')), in the same call.")] = False,
        with_live_view: Annotated[bool, Field(description="Also return the live view after the run (as fitnesse_debug_inspect(kind='liveView')), in the same call.")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """(nextPage +) pageHtml + runTable (one per table until a failure) via responder=debugTest."""
        if setup_only and tables is not None:
            raise ToolError("Pass either setup_only=true or tables, not both.")
        if from_table is not None and (setup_only or tables is not None):
            raise ToolError("from_table continues the whole page: pass it without tables and setup_only.")
        state = _require_session()
        current = state.current_page
        if not state.suite and current and current == state.suite_setup:
            current = state.page  # single page: the SuiteSetUp only leads to the page
        page = _page_key(page_path) or current or state.page
        if page_path:
            _check_runnable(state, page)

        def run() -> dict:
            html_page = _page_html(state, page, timeout_seconds)
            parsed = _parse_tables(html_page)
            parts = [html_page[t["start"]:t["end"]] for t in parsed]
            whole = tables is None and not setup_only
            if whole:
                state.table_count = len(parts)
            skipped: list[int] = []
            if setup_only:
                chosen = _setup_indices(parsed)
                if not chosen:
                    return {"page": page, "setup_tables": [], "hint": "The page has no SetUp tables; nothing was run."}
            elif tables is not None:
                bad = [i for i in tables if not 0 <= i < len(parts)]
                if bad:
                    raise ToolError(f"Table indices {bad} out of range: the page has {len(parts)} tables.")
                chosen = list(tables)
            else:
                first = state.next_table if from_table is None else from_table
                if first > len(parts):
                    raise ToolError(f"from_table {first} out of range: the page has {len(parts)} tables.")
                if state.setup_ran == page:  # SetUp already ran in this test
                    skipped = [i for i in _setup_indices(parsed) if i >= first]
                chosen = [i for i in range(first, len(parts)) if i not in skipped]
                if not chosen:
                    state.next_table = len(parts)
                    result = {"page": page, "page_done": True, "hint": (
                        "All tables of this page have run. Go on with the next page (suite), "
                        "restart=true to run it again, or fitnesse_debug_end.")}
                    return {**result, "skipped_setup": skipped} if skipped else result
            if stop_on_failure:
                data, ran, failed = _run_tables_stepwise(state, parts, chosen, timeout_seconds)
            else:
                html = "\n".join(parts[i] for i in chosen) if chosen != list(range(len(parts))) else html_page
                data = _session_action(state, "runTable", _run_form({"html": html}, with_variables, with_live_view),
                                       timeout_seconds=timeout_seconds)
                ran, failed = chosen, None
            complete = failed is None or failed == chosen[-1]
            _seen_page(state, parsed, ran)
            if (setup_only and complete) or (tables is not None and _covers_setup(html_page, ran)):
                state.setup_ran = page  # SetUp ran in this test: a later whole-page run skips it
            if state.setup_ran == page or whole:
                state.setup_seen = page
            if whole:
                state.next_table = ran[-1] + 1
            elif setup_only and failed is not None:  # continue after it, through the rest of the SetUp
                state.next_table, state.table_count = failed + 1, len(parts)
            result = _document_result(state, data, page, include_html, only_failures, ran)
            if skipped:
                result["skipped_setup"] = skipped
            if failed is not None:
                result["stopped_at"] = _stop_details(state, html_page, parsed, page, failed, timeout_seconds)
                if stop_on_failure:  # the failing table is the last: its screenshot, as an image
                    result["_images"] = result.get("_images", []) + _failure_screenshot(data.get("html") or "")
                rest = len(chosen) - len(ran)
                if whole or setup_only:
                    result["next_table"] = state.next_table
                    result["hint"] = (
                        f"Stopped after table {failed} failed; the rest of the page (TearDown included) "
                        "has not run. Try corrected versions of stopped_at.wiki with "
                        "fitnesse_debug_run_wiki (saves nothing) "
                        "until one passes and save that one once with fitnesse_table_edit (stopped_at's "
                        "source_page/source_index), or report that there is no fix; then call this "
                        f"without tables to continue at table {state.next_table}.")
                elif rest:
                    result["tables_not_run"] = chosen[len(ran):]
            elif whole:
                result["page_done"] = True
            if tables is not None and state.table_count is not None and state.next_table < len(parts):
                result["next_table"] = state.next_table  # the page's run stopped before: where it goes on
                result.setdefault("hint", (
                    f"The page's run is still stopped: call this without tables to continue it at table "
                    f"{state.next_table}."))
            return _with_extras(state, data, result, with_variables, with_live_view, timeout_seconds)

        with _document(state):
            suite_setup = _run_suite_setup(state, page, stop_on_failure, timeout_seconds, restart)
            if suite_setup is not None and "stopped_at" in suite_setup:
                failed, setup_page = suite_setup["stopped_at"]["index"], state.suite_setup
                return _with_images({"page": page, "suite_setup": suite_setup, "hint": (
                    f"The SuiteSetUp {setup_page} stopped after its table {failed} failed; the test of "
                    f"{page} has not begun. Try corrected versions of stopped_at.wiki with "
                    "fitnesse_debug_run_wiki (saves "
                    "nothing) until one passes and save that one once with fitnesse_table_edit "
                    "(stopped_at's source_page/source_index), or report that there is no fix; then "
                    "call this again: it continues the "
                    f"SuiteSetUp at table {suite_setup['next_table']} (restart=true: from its first "
                    f"table), then runs {page}.")})
            result = _run_document(state, page, restart and suite_setup is None, timeout_seconds, run, skip_teardown)
            if page == state.suite_setup and result.get("page_done"):
                state.suite_setup_pending = False
            if suite_setup is not None:
                result = {"suite_setup": suite_setup, **result}
            return _with_images(result)

    @mcp.tool(
        name="fitnesse_debug_inspect",
        description=(
            "Looks into the debug session. kind:\n"
            "- 'keywords': the keywords usable in tables now. Without filter only the categories "
            "and their sizes. With filter (a word such as 'click', or a category; help texts are "
            "searched too) the matching entries: syntax, where %1, %2 are the parameter cells "
            "('click %1' -> '|click|//button|'), parameters (label, options), returnType and "
            "tableItems (the rows the keyword expects in the rest of its table). Write the syntax "
            "as given, !-JavaScript-! included. generated=true: a method without keyword "
            "annotation, callable as written. details=true adds the help texts. Defined actions "
            "are not in it.\n"
            "- 'definedActions': the defined actions the session has loaded with |define actions "
            "at| (usually in the SetUp): action (the call), parameters, note, deprecated, author, "
            "page and overrides. filter searches these, not the body: read that with "
            "fitnesse_debug_defined_action_source.\n"
            "- 'dynamicVariables': the variables set during the test (name, value).\n"
            "- 'scope': the objects whose keywords are available, in lookup order (level, name, "
            "class, depth).\n"
            "- 'liveView': the open remote browsers and live_view_url, a link where a person can "
            "watch the browser."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_inspect(
        kind: Annotated[Literal["keywords", "definedActions", "dynamicVariables", "scope", "liveView"], Field(description="What to look at; see the tool description.")],
        filter: Annotated[str | None, Field(description="Only for kind='keywords' or 'definedActions': text to search for (case-insensitive), e.g. 'browser'.")] = None,
        limit: Annotated[int, Field(description="Only for kind='keywords' with filter: maximum number of entries.", ge=1)] = 30,
        details: Annotated[bool, Field(description="Only for kind='keywords' with filter: also return the help texts (long).")] = False,
        include_system: Annotated[bool, Field(description="Only for kind='dynamicVariables': also list the unchanged Java system properties (many).")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """keywordsJson/keywords/dynamicVariables/scope/liveView via responder=debugTest."""
        state = _require_session()
        if kind == "definedActions":
            actions = _session_defined_actions(state, timeout_seconds)
            if not actions:
                return {"definedActions": [], "hint": _NO_DEFINED_ACTIONS}
            hits = [a for a in actions if _matches_action(json.dumps(a, ensure_ascii=False), filter)]
            result = {"count": len(actions), "matches": len(hits), "definedActions": hits[:limit]}
            if state.maybe_busy:  # _session_defined_actions answered from what it read before
                result["note"] = ("The test process is still running a call that timed out: this is the "
                                  "list as read before it; it may have changed since.")
            return result
        action = {"keywords": "keywordsJson"}.get(kind, kind)
        with _document(state):
            data = _session_action(state, action, timeout_seconds=timeout_seconds)
        html = data.get("html") or ""
        if kind == "keywords":
            return _keywords(html, filter, limit, details)
        if kind == "liveView":
            return _live_view(data)
        if kind == "dynamicVariables":
            return {kind: _variables(html, include_system)}
        return {kind: _query_records(html, kind), "note": _SCOPE_NOTE}

    @mcp.tool(
        name="fitnesse_debug_end",
        description=(
            "Ends the debug session as a normal run ends: first the current page's TearDown tables "
            "that have not run yet (teardown), on a single test page also its SuiteTearDown "
            "(suite_teardown), then the test process stops. Call it whenever you are done.\n"
            "Result: the tear down's tables (FitLibrary's own 'Messages in storytest tear down' is "
            "always among them), the last unread log entries (log; log_omitted counts "
            "those left out) and page_totals, the counts per page. Take the outcome from "
            "page_totals: the 'Exit code' in the log is the sum of the wrong and exception counts of "
            "everything the session ran, not an outcome.\n"
            "abort=true stops the process at once without tear down, also while another call is "
            "running: use it when the session hangs or busy stays true."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_end(
        abort: Annotated[bool, Field(description="Stop immediately without tear down, even while a call is running.")] = False,
        skip_teardown: Annotated[bool, Field(description="Leave out the TearDown tables that have not run yet and the SuiteTearDown, e.g. to keep the data a stop left behind; the process still ends.")] = False,
        max_log_lines: Annotated[int, Field(description="Maximum number of unread log lines to return, the last ones. 0 for none.", ge=0)] = 30,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """end/abort via responder=debugTest."""
        state = _require_session()
        frame: dict = {}
        before: dict = {}  # the current page's TearDown, when no SuiteTearDown frame ended it
        if not abort:
            with _document(state):
                if skip_teardown and _has_suite_teardown(state):
                    frame["suite_teardown_skipped"] = state.suite_teardown
                if skip_teardown or not _run_suite_teardown(state, frame, timeout_seconds):
                    try:
                        before = _end_test(state, skip_teardown, timeout_seconds)
                    except _TearDownFailed as exc:
                        before = {"teardown_error": str(exc)}
        total = _log_lines_total(state, 10.0)
        # logMax (mod #162) lets FitNesse cut the log itself; older forks ignore it
        form = {"logFrom": _log_from(total, state.log_next, max_log_lines), "logMax": max_log_lines or None}
        if abort:
            data = _session_action(state, "abort", form, timeout_seconds=30.0)
            result: dict = {"ended": data.get("ended", True), "aborted": True}
        else:
            with _document(state):
                data = _session_action(state, "end", form, timeout_seconds=timeout_seconds)
            result = {"ended": data.get("ended", True)}
            ended = _document_result(state, data, state.current_page, include_html=False)
            result.update(frame)
            if "suite_teardown" in frame:
                frame["suite_teardown"]["end"] = ended  # the SuiteTearDown's own storytest end
            elif "teardown" not in frame:
                result["teardown"] = {**ended, **before}
        log = data.get("log")
        entries = _log_entries(log.get("lines")) if isinstance(log, dict) else []
        kept = entries[len(entries) - max_log_lines:] if max_log_lines else []
        if kept:
            result["log"] = [_log_entry(e) for e in kept]
        # the unread lines logFrom skipped, those FitNesse left out for logMax, and those of the entries cut here
        start = form["logFrom"] if form["logFrom"] is not None else max(state.log_next, total or 0)
        skipped = log.get("skipped") if isinstance(log, dict) else None
        cut = sum(len(e["lines"]) for e in entries[:len(entries) - len(kept)])
        omitted = start - state.log_next + (skipped if isinstance(skipped, int) else 0) + cut
        if omitted:
            result["log_omitted"] = omitted
        result["page_totals"] = state.page_counts
        state.reset()
        return _with_images(result)

    @mcp.tool(
        name="fitnesse_debug_defined_action_source",
        description=(
            "Reads the wiki of a defined action for editing, fresh from the page that defines it: "
            "the definition block as FitLibrary reads it, from its header table to the next ---- "
            "line. A definition is a signature table |greet|name| (plain names) plus body tables using "
            "@{name}, ended by ----. Result: page, index, last and original, ready for "
            "fitnesse_table_edit(action='save', page_path=page, index=index, last=last, "
            "original=original, wiki=<new>, reload_defined_actions=true). Needs a debug session "
            "that has loaded the action, or page_path, the page that defines it. To try a fix "
            "before saving, run the body's rows with fitnesse_debug_run_wiki, its parameters "
            "replaced by values."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_debug_defined_action_source(
        action: Annotated[str, Field(description="The defined action: its call as in fitnesse_debug_inspect(kind='definedActions'), e.g. '|click by js|locator|', or just its keywords, e.g. 'click by js'.")],
        page_path: Annotated[str | None, Field(description="Only without a debug session: the page that defines it (action then has to be the call row).")] = None,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """definedAction via responder=debugTable; on older forks keywords + source, cut here."""
        state = _debug_state()
        if state.session:
            defined = None
            call = action.strip()
            if not call.startswith("|"):  # bare keywords: the fork wants a call row
                defined = _session_defined_action(state, call, timeout_seconds)
                call = defined["action"]
            look_up = None
            try:
                found = _server_defined_action(_call_row(call), state)
            except _LookUpOnPage as exc:
                found, look_up = None, exc
            if found is not None:
                return found
            try:
                defined = defined or _session_defined_action(state, call, timeout_seconds)
                source = _debug_post("debugTable", defined["page"], "source", timeout_seconds=30.0)
                found = {"action": defined["action"], "page": defined["page"], **_definition_block(source, defined)}
                if look_up is not None and look_up.reason == "changed":
                    found["note"] = (
                        "The page changed since the session loaded it: this is the definition on the page "
                        "now, the session still runs the one it loaded. Save with "
                        "reload_defined_actions=true, or run_wiki('|define actions at|."
                        f"{defined['page']}|'), to load it."
                    )
                return found
            except ToolError as exc:
                if look_up is None:
                    raise
                if str(exc).startswith("No definition of"):
                    raise ToolError(
                        f"{look_up} {defined['page'] if defined else 'Its page'} no longer has {call}; the "
                        "session keeps the old definition "
                        + ("until the page's defined actions are loaded again (|define actions at|)."
                           if look_up.reason == "changed" else
                           "until it ends. Edit the page (fitnesse_table_source), or end the session and "
                           "start again to drop it.")
                    ) from exc
                raise ToolError(f"{look_up} Looking it up on the page failed as well: {exc}") from exc
        elif page_path:
            call = _call_row(action)
            keywords = [_plain(c) for c in _wiki_cells(call)[::2]]
            defined = {"action": call, "keywords": keywords, "page": page_path.strip().lstrip(".")}
        else:
            _require_session()  # raises with the hint
        source = _debug_post("debugTable", defined["page"], "source", timeout_seconds=30.0)
        return {"action": defined["action"], "page": defined["page"], **_definition_block(source, defined)}

    @mcp.tool(
        name="fitnesse_table_source",
        description=(
            "Reads a wiki page as its tables and the texts between them, for fitnesse_table_edit "
            "and fitnesse_table_edit_text. Result: tables (the wiki of each of the page's own "
            "tables; tables of included pages and tables nested in cells don't count) and texts: "
            "texts[i] is the text in front of tables[i], the last one follows the last table. "
            "These indices are not those of fitnesse_debug_page_tables, which also counts SetUp, "
            "TearDown and included pages. Needs no debug session."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_table_source(
        page_path: WikiPath,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """source via responder=debugTable."""
        return _debug_post("debugTable", page_path, "source", timeout_seconds=timeout_seconds)

    # debugTable saves wiki pages for real: tagged write, so they need FITNESSE_TOOLSETS=write.
    _TABLE_EDIT = (
        "Saves the page for real: change only pages you are asked to change. original is the "
        "current wiki from fitnesse_table_source, unchanged; if the page has changed since, the "
        "call fails: read it again and redo the change. The result's source is the page after the change (tables and texts, as "
        "fitnesse_table_source): take the next original and the indices from it. A running debug session sees "
        "the change when the tables run again."
    )
    _RELOAD = Annotated[bool, Field(description=(
        "After saving, load the page's defined actions into the running debug session again, so "
        "that it runs the new version (result: reload). For pages that define actions."))]

    @mcp.tool(
        name="fitnesse_table_edit",
        description=(
            "Changes the tables of a wiki page, one change per call. action:\n"
            "- 'save': replaces table index with wiki; with last, tables index..last and the text "
            "between them, e.g. a defined action of several tables.\n"
            "- 'insert': puts the table(s) of wiki in front of table index; index = number of "
            "tables appends them to the page.\n"
            "- 'delete': removes table index.\n"
            "- 'move': moves table index in front of table to, counted before the move; to = "
            "number of tables moves it to the end.\n"
            "insert and move put the table at the end of the text in front of the target table, "
            "with start=true at its beginning.\n" + _TABLE_EDIT
            + " A defined action: signature table |greet|name| plus body tables using @{name}, a ---- line "
            "after each definition. A reload adds, it never removes: after a wrong format start a new session."
        ),
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_table_edit(
        page_path: WikiPath,
        action: Literal["save", "insert", "delete", "move"],
        index: Annotated[int, Field(description="Index among the page's own tables, as in fitnesse_table_source.", ge=0)],
        original: Annotated[str, Field(description="tables[index] of fitnesse_table_source. save with last: tables index..last with the texts between them, as on the page. insert: the table now at index, empty when appending.")],
        wiki: Annotated[str | None, Field(description="save, insert: the new wiki markup.")] = None,
        last: Annotated[int | None, Field(description="save: last table of a block to replace.", ge=0)] = None,
        to: Annotated[int | None, Field(description="move: target position, counted before the move.", ge=0)] = None,
        start: Annotated[bool, Field(description="insert, move: go to the beginning of the text in front of the target table instead of its end.")] = False,
        reload_defined_actions: _RELOAD = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """save/saveBlock/insert/delete/move via responder=debugTable (+ reload of defined actions)."""
        _only_for(action, {"wiki": (wiki, ("save", "insert")), "last": (last, ("save",)),
                           "to": (to, ("move",)), "start": (start or None, ("insert", "move"))})
        if action == "save":
            form: dict = {"index": index, "last": last, "original": original, "wiki": _needed(wiki, "wiki", action)}
            return _save_and_reload(page_path, "save" if last is None else "saveBlock", form,
                                    timeout_seconds, reload_defined_actions)
        if action == "insert":
            form = {"index": index, "before": original, "start": start, "wiki": _needed(wiki, "wiki", action)}
        elif action == "move":
            form = {"index": index, "original": original, "to": _needed(to, "to", action), "start": start}
        else:
            form = {"index": index, "original": original}
        return _save_and_reload(page_path, action, form, timeout_seconds, reload_defined_actions)

    @mcp.tool(
        name="fitnesse_table_edit_text",
        description=(
            "Changes the wiki text between the tables of a page (headings, comments, collapsible "
            "sections), one change per call. texts[index] of fitnesse_table_source is the text in "
            "front of table index; the last one follows the last table. action:\n"
            "- 'save': replaces texts[index] with wiki. Tables in the new text are added (result: "
            "added); text that would change or remove a table is refused.\n"
            "- 'move': moves texts[index] behind texts[to], with start=true in front of it.\n"
            + _TABLE_EDIT
        ),
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_table_edit_text(
        page_path: WikiPath,
        action: Literal["save", "move"],
        index: Annotated[int, Field(description="Index into texts of fitnesse_table_source.", ge=0)],
        original: Annotated[str, Field(description="texts[index] of fitnesse_table_source.")],
        wiki: Annotated[str | None, Field(description="save: the new wiki text.")] = None,
        to: Annotated[int | None, Field(description="move: index into texts of the target text.", ge=0)] = None,
        to_original: Annotated[str | None, Field(description="move: texts[to] of fitnesse_table_source.")] = None,
        start: Annotated[bool, Field(description="move: put it in front of texts[to] instead of behind it.")] = False,
        reload_defined_actions: _RELOAD = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """saveText/moveText via responder=debugTable (+ reload of defined actions)."""
        _only_for(action, {"wiki": (wiki, ("save",)), "to": (to, ("move",)),
                           "to_original": (to_original, ("move",)), "start": (start or None, ("move",))})
        if action == "save":
            form: dict = {"index": index, "original": original, "wiki": _needed(wiki, "wiki", action)}
            return _save_and_reload(page_path, "saveText", form, timeout_seconds, reload_defined_actions)
        form = {"index": index, "original": original, "to": _needed(to, "to", action),
                "toOriginal": _needed(to_original, "to_original", action), "start": start}
        return _save_and_reload(page_path, "moveText", form, timeout_seconds, reload_defined_actions)


# The mode each tool needs: by tag, plus the few control tools by name.
# tests/test_server.py checks that every tool has exactly one.
MODE_TAGS: dict[str, set[str]] = {"read": {"read"}, "write": {"write"}, "test": {"execute"}}
MODE_NAMES: dict[str, set[str]] = {
    "read": set(),
    "write": {"fitnesse_shutdown", "fitnesse_save_account"},
    "test": {"fitnesse_stop_test"},
}

for _mode in set(MODES) - FITNESSE_TOOLSETS:
    mcp.add_transform(Visibility(False, tags=MODE_TAGS[_mode]))
    if MODE_NAMES[_mode]:
        mcp.add_transform(Visibility(False, names=MODE_NAMES[_mode]))
# The areas complete and debug gate registration (COMPLETE_TOOLSET, DEBUG_TOOLSET above).


# Sent to the client at initialize; agents see it, the README they do not. It holds what spans
# several tools; what one tool does and returns is in that tool's description.
DEBUG_INSTRUCTIONS = """\
KDTA debug tools, how they work together:
- A session comes first: fitnesse_debug_start{adopt_start}. Only fitnesse_debug_status\
{adopt_status}, fitnesse_debug_steps, fitnesse_debug_read_link and the fitnesse_table_* tools work\
 without one.{adopt_none}
- Flow: start -> run_page(setup_only=true) -> inspect(kind='keywords' / 'definedActions') -> \
run_wiki / run_page, repeatedly -> end. Without the SetUp there are no defined actions, no \
browser and often no fixtures. Always end the session: it holds server resources.
- run_page stops after the first table with a [fail] or [error] cell (stopped_at, next_table). \
Find the cause (the screenshot that comes with it, inspect, fitnesse_debug_read_link for the \
'HTML' link), try corrected versions of the table with run_wiki, which saves nothing, and save \
the one that passes, or report that there is no fix. A table that stopped partway can be run \
again. Then run_page() continues at next_table. \
Go on to the next page only when page_done is true.
- State (browser, scope, variables) carries over between calls on the same page. run_page on \
another page, restart=true and end first end the current page's test as a normal run would: its \
TearDown tables that have not run yet run then; skip_teardown=true leaves them out.
- Each |with browser| table starts its own browser, kept until fitnesse_debug_end: use |add global|with browser| in the SetUp.
- Building a test or a SetUp: run_wiki notes each call whose tables all pass as a step \
(record=false for tables that only look around); fitnesse_debug_steps returns their wiki, to save on the page in one go.
- ${{VARIABLES}} in run_wiki are those of the current page; fitnesse_get_variables lists them.
- busy=true in fitnesse_debug_status: a call that timed out is still running. Poll status, or fitnesse_debug_end(abort=true); run_wiki and run_page fail at once meanwhile. \
That call's result is lost: once busy is false, see in the status log how far it got and go \
on with run_page(from_table=...), or restart=true.
""".format(
    adopt_start=" (or fitnesse_debug_status(adopt=true) for a running session)" if ALLOW_ADOPT else "",
    adopt_status=" without adopt" if ALLOW_ADOPT else "",
    adopt_none="" if ALLOW_ADOPT else " Sessions this server did not start (someone else's, or "
    "from before a restart) are never taken over: leave them alone.",
)

# fitnesse_table_source needs debug and read, like the base part.
DEBUG_TABLE_SOURCE_INSTRUCTIONS = """\
- Two ways of counting tables: fitnesse_debug_page_tables and run_page count every table a run \
executes (SetUp, TearDown, included pages); fitnesse_table_source and the edit tools count only \
the page's own tables. Never pass an index from one to the other: a session's table has \
source_page and source_index for editing.
"""

# Only with write as well, where fitnesse_table_edit and fitnesse_table_edit_text are visible.
DEBUG_TABLE_EDIT_INSTRUCTIONS = """\
- A new test page has to exist before fitnesse_debug_start: create it with fitnesse_add_child_page.
- Editing: read fitnesse_table_source and pass the table or text to change, unchanged, as \
original to fitnesse_table_edit / fitnesse_table_edit_text. Each result has the page after the \
change (source): take the next original and indices from it. A running session sees the change \
when the tables run again.
- Changing a defined action: fitnesse_debug_defined_action_source(action) -> \
fitnesse_table_edit(action='save', ..., reload_defined_actions=true) -> run it again. A new one: \
fitnesse_table_edit(action='insert', ..., reload_defined_actions=true) on a page that the SetUp \
loads with |define actions at|; a ---- line between two definitions is text, added with \
fitnesse_table_edit_text. reload.hint says what to do if it did not load.
"""

# Each part names only tools the toolsets make visible: the base part needs the session
# tools (test) and status/page_tables/inspect (read).
if {"debug", "read", "test"} <= FITNESSE_TOOLSETS:
    mcp.instructions = DEBUG_INSTRUCTIONS + DEBUG_TABLE_SOURCE_INSTRUCTIONS
    if "write" in FITNESSE_TOOLSETS:
        mcp.instructions += DEBUG_TABLE_EDIT_INSTRUCTIONS

# What debug loses without each mode; a debug area that can't open a session is likely a mistake.
_DEBUG_WITHOUT = {
    "test": "fitnesse_debug_start, _run_wiki, _run_page and _end are hidden, so no debug session can be started",
    "read": "fitnesse_debug_status, _page_tables, _inspect, _defined_action_source and fitnesse_table_source are hidden",
}
if DEBUG_TOOLSET and not {"read", "test"} <= FITNESSE_TOOLSETS:
    logger.warning(
        "FITNESSE_TOOLSETS has debug without %s: %s; clients get no debug instructions. "
        "Use read,test,debug (plus write to edit tables) for the full debug mode.",
        " and ".join(m for m in ("read", "test") if m not in FITNESSE_TOOLSETS),
        "; ".join(_DEBUG_WITHOUT[m] for m in ("read", "test") if m not in FITNESSE_TOOLSETS),
    )



def main() -> None:
    """Entry point of the fitnesse-mcp command: stdio unless FASTMCP_TRANSPORT says otherwise."""
    import fastmcp

    if FITNESSE_AUTH_MODE == "headers" and fastmcp.settings.transport == "stdio":
        raise SystemExit("FITNESSE_AUTH_MODE=headers needs the HTTP transport (FASTMCP_TRANSPORT=http).")
    mcp.run()


if __name__ == "__main__":
    main()
