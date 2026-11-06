# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT

import binascii
import json
import logging
import mimetypes
import os
import re
import threading
import uuid
import xml.etree.ElementTree as ET
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
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
        description="Path under the FitNesse files section, e.g. 'files/images'. Must start with the resource root configured via FITNESSE_FILES_ROOT (default 'files'). No leading slash."
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
            raw_bytes = response.read(MAX_BYTES + 1)
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
        error_body = exc.read(MAX_BYTES).decode("utf-8", errors="replace")
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
            raw_bytes = response.read(MAX_TRANSFER_BYTES + 1)
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
            "body": exc.read(MAX_BYTES).decode("utf-8", errors="replace"),
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
    description="Creates a new child page beneath the selected page using pageName and pageContent, with optional pageTemplate, pageType, help text and tags (suites).",
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False},
)
def fitnesse_add_child_page(
    parent_page_path: WikiPath,
    page_name: PageName,
    page_content: PageContent = "",
    page_template: str | None = None,
    page_type: PageType | None = None,
    help_text: str | None = None,
    suites: Annotated[str | None, Field(description="Comma-separated tags for the new page.")] = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Create a child page using responder=addChild."""
    params: dict[str, str | None] = {
        "responder": "addChild",
        "pageName": page_name,
        "pageContent": page_content,
    }
    if page_template:
        params["pageTemplate"] = page_template
    if page_type:
        params["pageType"] = page_type
    if help_text:
        params["helpText"] = help_text
    if suites:
        params["suites"] = suites
    return _request(
        "GET", parent_page_path, params=params, timeout_seconds=timeout_seconds
    )


@mcp.tool(
    name="fitnesse_compare_history",
    description="Generates a report comparing two test result files for a page history responder call.",
    tags={"fitnesse", "rest", "history", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_compare_history(
    page_path: WikiPath,
    first_result_file: str,
    second_result_file: str,
    timeout_seconds: float = _make_timeout(60.0),
) -> dict:
    """Compare two history result files via responder=compareHistory."""
    # list of tuples preserves both params even when both filenames are equal
    return _request(
        "GET",
        page_path,
        params=[
            ("responder", "compareHistory"),
            (first_result_file, None),
            (second_result_file, None),
        ],
        timeout_seconds=timeout_seconds,
    )


if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_create_dir",
        description="Creates a new directory in the files section below the given resource using dirname.",
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
    description="Deletes the specified page.",
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
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_delete_file",
        description="Deletes a file from the files section directory identified by the resource and filename.",
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
            "Files, Search) are only matched by FitNesse 20261101 or later."
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
        params: dict[str, str | None] = {"responder": "searchProperties"}
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
        return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)

if os.getenv("FITNESSE_FILES_ROOT"):

    @mcp.tool(
        name="fitnesse_list_files",
        description="Displays a directory listing in the files section for the selected files resource. Returns result as JSON.",
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
        description="Downloads a file from the files section directory identified by the resource and filename, returning its content directly as an image or text/binary resource.",
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


@mcp.tool(
    name="fitnesse_import_pages",
    description="Imports a page hierarchy from a foreign FitNesse using remoteUrl with optional credentials and autoUpdate.",
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


@mcp.tool(
    name="fitnesse_import_and_view",
    description="Imports a page hierarchy if needed and then views the selected page.",
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
    description="Moves the selected page below a different parent page using newLocation.",
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False},
)
def fitnesse_move_page(
    page_path: WikiPath,
    new_location: str,
    refactor_references: Annotated[
        bool, Field(description="Also update references to the moved page.")
    ] = False,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Move a page under another parent via responder=movePage."""
    params: dict[str, str | None] = {"responder": "movePage", "newLocation": new_location}
    if refactor_references:
        params["refactorReferences"] = None
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


@mcp.tool(
    name="fitnesse_list_names",
    description="Lists page names at the current level and supports Recursive, LeafOnly, ShowTags, and ShowChildCount. Returns JSON.",
    tags={"fitnesse", "rest", "pages", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_list_names(
    page_path: WikiPath = "",
    recursive: bool = False,
    leaf_only: bool = False,
    show_tags: bool = False,
    show_child_count: bool = False,
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
        "Returns the raw wiki text of the selected page via pageData responder (body), plus "
        "edit_time and ticket_id. Pass both to fitnesse_save_page_content when saving changes "
        "based on this text: if someone else saved the page in between, the save fails "
        "instead of overwriting their change."
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
    description="Displays test history for a page with optional resultDate selector. Returns XML.",
    tags={"fitnesse", "rest", "history", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_page_history(
    page_path: WikiPath,
    result_date: str | None = None,
    include_command: bool = False,
    include_stdout: bool = False,
    include_stderr: bool = False,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Get page history via responder=pageHistory.

    The executionLog's command, stdOut and stdErr are dropped unless requested;
    stdOut alone can be megabytes and break MCP clients.
    """
    params: dict[str, str | None] = {"responder": "pageHistory", "format": "xml"}
    if result_date:
        params["resultDate"] = result_date
    result = _request_bytes("GET", page_path, params=params, timeout_seconds=timeout_seconds)
    if not result["ok"]:
        return result
    raw: bytes = result["body"]
    if result["truncated"]:
        logger.warning("Page history from %s not filtered: response was truncated", result["url"])
    else:
        raw = _strip_execution_log(
            raw,
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
    if len(raw) > MAX_BYTES:
        logger.warning(
            "Page history from %s is %d bytes after filtering, truncated to "
            "FITNESSE_MAX_RESPONSE_BYTES=%d",
            result["url"],
            len(raw),
            MAX_BYTES,
        )
        result["truncated"] = True
    result["body"] = raw[:MAX_BYTES].decode("utf-8", errors="replace")
    return result


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
    description="Displays the properties form for the selected page. Returns JSON.",
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


@mcp.tool(
    name="fitnesse_purge_history",
    description="Purges test history files older than `days` for the selected page and its children; purge_global=true purges the history of all pages instead.",
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
        description="Renames a file in the files section using filename and newName within the selected directory.",
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
    description="Renames the selected page using newName and does not perform page moves.",
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
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


@mcp.tool(
    name="fitnesse_rollback_version",
    description="Rolls back a page to a selected version identifier from saved versions.",
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": True},
)
def fitnesse_rollback_version(
    page_path: WikiPath,
    version: str,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Rollback to a saved version via responder=rollback."""
    return _request(
        "GET",
        page_path,
        params={"responder": "rollback", "version": version},
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(
    name="fitnesse_get_rss",
    description="Returns an RSS feed for the current page and all of its children.",
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
        "Saves wiki page content via POST using pageContent and optional editTime, ticketId, "
        "and redirect fields. FitNesse removes the page's help text and tags when they are "
        "not sent, so help_text and suites are kept from the current page unless given; "
        "pass an empty string to remove them. Without edit_time the save overwrites the page "
        "(last write wins). With edit_time and ticket_id (from fitnesse_get_page_data) "
        "FitNesse checks for a conflict: if someone else saved the page since then, nothing "
        "is saved and the tool fails; re-read the page, merge, and save again."
    ),
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_save_page_content(
    page_path: WikiPath,
    page_content: PageContent,
    edit_time: Annotated[str | None, Field(description="edit_time returned by fitnesse_get_page_data; enables the conflict check. Omit to overwrite.", pattern=r"^\d+$")] = None,
    ticket_id: Annotated[str | None, Field(description="ticket_id returned by the same fitnesse_get_page_data call; saves with the same ticket never conflict with each other.", pattern=r"^-?\d+$")] = None,
    redirect: str | None = None,
    help_text: str | None = None,
    suites: str | None = None,
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
    return result


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
        "Searches pages for searchString in page names (search_type='title') or page content "
        "(search_type='content'). Text search is plain text, not a regex. method_search=true "
        "(content only) finds pages using a method or scenario: give a wiki line that evaluates to "
        "it, e.g. '|click|x|'. Searches page_path and its children, unless search_scope names an "
        "ancestor page or 'root' for the whole wiki. Returns an HTML table of matches."
    ),
    tags={"fitnesse", "rest", "search", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_search(
    search_string: str,
    page_path: WikiPath = "",
    search_type: Literal["title", "content"] = "content",
    method_search: bool = False,
    search_scope: Annotated[
        str | None,
        Field(description="Ancestor page to search below, or 'root' for the whole wiki."),
    ] = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Search pages with responder=search."""
    if method_search and search_type != "content":
        raise ToolError("method_search only works with search_type='content'")
    params: dict[str, str | None] = {
        "responder": "search",
        "searchString": search_string,
        "searchType": search_type,
    }
    if method_search:
        params["isMethodSearch"] = None
    if search_scope:
        params["searchScope"] = search_scope
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)

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
    description="Stops running tests, either all active tests or one process when id is provided.",
    tags={"fitnesse", "rest", "tests", "admin", "control"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_stop_test(
    test_process_id: str | None = None,
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
        "Runs all tests below the selected page. Select tests with tags (suite_filter / "
        "run_tests_matching_any_tag: any of the tags, the latter wins; run_tests_matching_all_tags; "
        "exclude_suite_filter), first_test, or a partition (partition_count + zero-based "
        "partition_index; preview with fitnesse_partition_preview). Returns XML unless format "
        "says otherwise. Do not combine nohistory with format='xml': the XML report is built "
        "from the history."
    ),
    tags={"fitnesse", "rest", "tests", "execute"},
    annotations={"destructiveHint": False},
)
def fitnesse_run_suite(
    page_path: WikiPath,
    debug: bool = False,
    remote_debug: bool = False,
    suite_filter: TagList = None,
    exclude_suite_filter: TagList = None,
    first_test: str | None = None,
    nohistory: bool = False,
    includehtml: bool = False,
    nochunk: bool = False,
    variables: dict[str, str] | None = None,
    run_tests_matching_any_tag: TagList = None,
    run_tests_matching_all_tags: TagList = None,
    partition_count: Annotated[int | None, Field(ge=1)] = None,
    partition_index: Annotated[int | None, Field(ge=0)] = None,
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
    if remote_debug:
        params["remote_debug"] = None
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


@mcp.tool(
    name="fitnesse_manage_symlink",
    description="Creates, removes, or renames symbolic links between pages using removal, rename, newname, linkName, and linkPath.",
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
    description="Runs a test page and supports debug, remote_debug, nochunk, nohistory, and markup variable inputs. Returns XML unless format says otherwise.",
    tags={"fitnesse", "rest", "tests", "execute"},
    annotations={"destructiveHint": False},
)
def fitnesse_run_test(
    page_path: WikiPath,
    debug: bool = False,
    remote_debug: bool = False,
    nochunk: bool = False,
    variables: dict[str, str] | None = None,
    nohistory: bool = False,
    format: RunFormat = "xml",
    timeout_seconds: float | None = _make_run_timeout(),
) -> dict:
    """Run a test with responder=test and optional controls."""
    params: dict[str, str | None] = {"responder": "test", "format": format}
    if debug:
        params["debug"] = None
    if remote_debug:
        params["remote_debug"] = None
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
        description="Uploads file content into the files section directory selected by the resource path. Pass text content directly via `content`, or binary/base64-encoded content via `content_base64`.",
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
    name="fitnesse_get_versions",
    description="Returns the versions view that lists saved versions for the selected wiki page.",
    tags={"fitnesse", "rest", "history", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_versions(
    page_path: WikiPath,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """List saved versions via responder=versions."""
    return _request(
        "GET",
        page_path,
        params={"responder": "versions"},
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(
    name="fitnesse_view_version",
    description="Shows a selected saved page version identified by the version file id.",
    tags={"fitnesse", "rest", "history", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_view_version(
    page_path: WikiPath,
    version: str,
    variables: dict[str, str] | None = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """View one saved version via responder=viewVersion."""
    return _request(
        "GET",
        page_path,
        params=_merge_extra({"responder": "viewVersion", "version": version}, variables),
        timeout_seconds=timeout_seconds,
    )


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
    description="Returns the variables available on the selected page context.",
    tags={"fitnesse", "rest", "pages", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_variables(
    page_path: WikiPath,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Return variables available on a page via responder=variables."""
    return _request(
        "GET",
        page_path,
        params={"responder": "variables"},
        timeout_seconds=timeout_seconds,
    )


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
    name="fitnesse_compare_versions",
    description=(
        "Shows the differences between two saved versions of a page, or between one version and "
        "the latest when only one is given. Version ids as listed by fitnesse_get_versions. "
        "Returns HTML."
    ),
    tags={"fitnesse", "rest", "history", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_compare_versions(
    page_path: WikiPath,
    versions: Annotated[list[str], Field(min_length=1, max_length=2)],
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Diff versions via responder=compareVersions (Version_<id> flags)."""
    params: list[tuple[str, str | None]] = [("responder", "compareVersions")]
    params += [(f"Version_{version}", None) for version in versions]
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


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
        "Shows the test pages of a suite as a tree with their latest results, without running "
        "anything. Takes the same test selection as fitnesse_run_suite. Returns HTML."
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
    timeout_seconds: float = _make_timeout(60.0),
) -> dict:
    """Suite tree with latest results via responder=overview."""
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
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_execution_log",
        description=(
            "Shows the execution log of a test run of the page: test system, command, exit code, "
            "stdout/stderr and exceptions. result_date is a timestamp (yyyyMMddHHmmss, as in "
            "fitnesse_get_page_history) or 'latest' (default). Returns HTML."
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
        return _request(
            "GET",
            page_path,
            params={"responder": "executionLog", "resultDate": result_date},
            timeout_seconds=timeout_seconds,
        )

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
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        self.session = self.page = self.current_page = self.setup_ran = self.setup_seen = None
        self.suite = False
        self.log_next = 0
        self.page_counts = {}


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
    "unknownPlace": "It was defined by a |define action| table, not on a page of defined actions: "
                    "edit that table where it is (fitnesse_table_source of its page).",
    "sessionRequired": "Several debug sessions of this account are running and this server knows none of "
                       "them: fitnesse_debug_status lists them in 'sessions'; continue one with "
                       "fitnesse_debug_status(adopt=true, session=...) or start one with fitnesse_debug_start.",
    "tooManySessions": "Too many debug sessions (debugTest.maxSessionsPerUser per account, debugTest.maxSessions "
                       "on the server). End the ones no longer needed: fitnesse_debug_status lists this "
                       "account's in 'sessions'.",
}


def _debug_post(
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
            raw_bytes = response.read(DEBUG_MAX_BYTES + 1)
    except TimeoutError as exc:
        raise ToolError(
            f"No answer to '{action}' within {timeout_seconds}s. The test process may still be "
            "running it: check fitnesse_debug_status, which also shows the log."
        ) from exc
    except URLError as exc:
        raise ToolError(f"Connection error: {exc.reason}") from exc

    if state is not None and headers is not None:
        for header in headers.get_all("Set-Cookie") or []:
            pair = header.split(";", 1)[0].strip()
            if pair.startswith(f"{_DEBUG_COOKIE}="):
                state.cookie = pair
    if len(raw_bytes) > DEBUG_MAX_BYTES:
        logger.warning("Response from %s exceeds FITNESSE_DEBUG_MAX_RESPONSE_BYTES=%d", url, DEBUG_MAX_BYTES)
        raise ToolError(
            f"Response to '{action}' exceeds FITNESSE_DEBUG_MAX_RESPONSE_BYTES={DEBUG_MAX_BYTES}."
        )
    content_type = (headers.get("Content-Type", "") if headers is not None else "") or ""
    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace")) if "json" in content_type else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        raise ToolError(
            f"FitNesse answered '{action}' with HTTP {status} and {content_type or 'no content type'} "
            "instead of JSON. Most likely the login is missing, expired or wrong (set "
            "FITNESSE_USERNAME/FITNESSE_PASSWORD or the X-FitNesse-* headers; Basic Auth bypasses the SSO), or this FitNesse "
            f"has no {responder} responder."
        )
    if status >= 400:
        code = data.get("code")
        message = data.get("error") or f"HTTP {status}"
        hint = _DEBUG_HINTS.get(status, "")
        if status == 409 and code in {"noSession", "replaced"} and state is not None:
            state.reset()
            hint = (
                "The debug session was replaced by a new one (on the same page; on older forks anywhere), "
                "e.g. from a browser with the same account. This MCP server forgot it; start a new one "
                "with fitnesse_debug_start."
                if code == "replaced"
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
        raise ToolError(f"HTTP {status}{detail}: {message}. {hint}".strip())
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
                self._cell = {"parts": [], "status": status}
                self.tables[-1]["rows"][-1].append(self._cell)
            else:
                self._text(" | ")
        elif tag == "div" and self._depth == 0:
            self._divs.append({"collapsible": "collapsible" in classes, "include": None})
            if self._fold is not None:
                self._fold["divs"] += 1
                self._text("\n")
            elif classes & _FOLDING_CLASSES:
                self._fold = {"divs": 1, "title": [], "parts": [], "title_tag": None, "owner": self._owner()}
        elif tag in {"br", "p", "div", "li", "pre"}:
            self._text("\n")
        if tag == "p" and self._depth == 0 and "title" in classes:
            self._title = True
        if self._fold is not None and self._depth == 0 and any((tag, c) in _FOLDING_TITLE for c in classes):
            self._fold["title_tag"] = tag

    def handle_startendtag(self, tag, attrs):
        if tag in {"img", "a"}:
            self._media(tag, dict(attrs))
            self._link = None
        elif tag == "br":
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
            [{"text": "".join(c["parts"]), "status": c["status"]} for c in row]
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
    return any(cell["status"] in ("fail", "error") for row in table["rows"] for cell in row)


def _tables_text(html: str, document: dict | None = None, only_failures: bool = False) -> str:
    """Condense annotated FitLibrary HTML into one text block per table.

    Cells carry their status as a prefix: [pass], [fail], [error], [ignore]. Folding areas,
    links and images follow the table they belong to. only_failures keeps just the tables with
    a [fail] or [error] cell, under their usual numbers.
    """
    document = document or _parse_document(html)
    blocks = []
    leading = [] if only_failures else _extras_lines(document["leading"], "Before the first table")
    if leading:
        blocks.append("\n".join(leading))
    for i, table in enumerate(document["tables"]):
        if only_failures and not _failed(table):
            continue
        lines = [f"Table {i}:"]
        for row in table["rows"]:
            cells = []
            for cell in row:
                text = _squash(cell["text"]).replace("\n", " ⏎ ")
                cells.append(f"[{cell['status']}] {text}" if cell["status"] else text)
            lines.append("| " + " | ".join(cells) + " |")
        lines.extend(_extras_lines(table, f"Table {i}"))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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


def _with_images(result: dict) -> dict | ToolResult:
    """Attach the inline images of result (and its tear-down parts) as MCP image content.

    Each document part lists its images in "images" with the content index they got, or why not.
    """
    attached: list[Image] = []
    for part in (result.get("previous_page_teardown"), result.get("teardown"), result):
        if not isinstance(part, dict):
            continue
        listed = []
        for image in part.pop("_images", []):
            entry = {"image": image["n"], "format": image["format"]}
            if image["data"] is None:
                entry["omitted"] = "invalid base64"
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
            "This MCP server has no debug session on this FitNesse. Start one with "
            "fitnesse_debug_start, or continue a running one with fitnesse_debug_status(adopt=true)."
        )
    return state


def _session_action(
    state: _DebugState,
    action: str,
    form: Mapping[str, str | int | bool | None] | None = None,
    timeout_seconds: float | None = 30.0,
) -> dict:
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
    state: _DebugState, data: dict, page: str | None, include_html: bool, only_failures: bool = False
) -> dict:
    html = data.get("html") or ""
    document = _parse_document(html)
    result = {
        "page": page,
        "counts": data.get("counts"),
        "page_totals": _add_counts(state, page, data.get("counts")),
        "tables": _tables_text(html, document, only_failures),
    }
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


def _with_setup_hint(state: _DebugState, result: dict, timeout_seconds: float | None) -> dict:
    """run_wiki before the SetUp: a failure there is often a defined action, browser or fixture
    the SetUp would have provided. Only for pages that have a SetUp."""
    counts = result.get("counts") or {}
    page = state.current_page or state.page
    if not page or state.setup_seen == page or not (counts.get("wrong") or counts.get("exceptions")):
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


def _next_page(state: _DebugState, page: str | None, timeout_seconds: float | None) -> dict:
    previous = state.current_page
    data = _session_action(state, "nextPage", {"page": page}, timeout_seconds=timeout_seconds)
    teardown = _document_result(state, data, previous, include_html=False)
    teardown["teardown_of"] = teardown.pop("page")
    state.current_page = data.get("page") or page or previous
    state.page_counts.pop(state.current_page, None)
    state.setup_ran = state.setup_seen = None  # a new test begins: its SetUp has not run
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
    page = page_path.strip().lstrip(".")
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
            f"{here['session']}, started {here.get('startedAt')}). Continue it with "
            f"fitnesse_debug_status(adopt=true, session='{here['session']}'), end it, or pass "
            "replace=true to kill it without tear down (its browser tab then shows 'replaced'). Sessions "
            "on other pages are not affected."
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


def _page_html(state: _DebugState, page: str, timeout_seconds: float | None) -> str:
    return _session_action(state, "pageHtml", {"page": page}, timeout_seconds=timeout_seconds).get("html") or ""


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


def _keywords(html: str, filter: str | None, limit: int) -> dict:
    rows = _query_rows(html)
    cells = rows[0] if rows else []
    try:
        keywords = json.loads(cells[1]) if len(cells) > 1 and cells[1] else []
        special = json.loads(cells[2]) if len(cells) > 2 and cells[2] else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"Unexpected keywordsJson answer: {exc}") from exc
    found = [
        {"category": " / ".join(path), "special": is_special, **entry}
        for is_special, source in ((False, keywords), (True, special))
        for path, entry in _walk_keywords(source)
    ]
    if filter:
        needle = filter.lower()
        hits = [e for e in found if needle in json.dumps(e, ensure_ascii=False).lower()]
        return {"matches": len(hits), "keywords": hits[:limit]}
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

_NO_DEFINED_ACTIONS = (
    "No defined actions loaded in this session. They are loaded by |define actions at|.Page| "
    "tables, usually in SetUp: find them with fitnesse_debug_page_tables and run them with "
    "fitnesse_debug_run_page(tables=[...])."
)
_MARKUP = re.compile(r"</?\w+>|'{2,3}")
_SEPARATOR = re.compile(r"(?m)^\s*-{4,}\s*$")


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
    with _document(state):
        try:
            data = _session_action(state, "definedActions", timeout_seconds=timeout_seconds)
        except ToolError as exc:
            if not _unknown_action(exc):
                raise
        else:
            return [_json_defined_action(a) for a in data.get("definedActions") or [] if isinstance(a, dict)]
        data = _session_action(state, "keywords", timeout_seconds=timeout_seconds)
    return _defined_actions(data.get("html") or "")


def _short_action(action: dict) -> dict:
    """What a reload reports per defined action: the call, plus note/author/deprecated if set."""
    return {"action": action["action"], **{k: action[k] for k in ("note", "author", "deprecated") if action.get(k)}}


def _matches_action(text: str, needle: str | None) -> bool:
    return not needle or needle.lower() in text.lower()


def _definition_block(source: Mapping, action: dict) -> dict:
    """The block of a page that defines action, found as FitLibrary reads the page.

    The header is a table whose first row's keyword cells (every other cell, starting with the
    first) match the action's keywords, and that is the page's first table or comes right after
    a ---- line. The block runs to the table before the next ----, with the text in between.
    """
    tables = list(source.get("tables") or [])
    texts = list(source.get("texts") or [])
    wanted = [k.lower() for k in action["keywords"]]
    starts = [
        i for i, table in enumerate(tables)
        if (i == 0 or _SEPARATOR.search(texts[i] if i < len(texts) else ""))
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
            f"{starts}): refusing to pick one. Edit the page with fitnesse_table_source / fitnesse_table_save."
        )
    first = last = starts[0]
    while last + 1 < len(tables) and not _SEPARATOR.search(texts[last + 1] if last + 1 < len(texts) else ""):
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


def _server_defined_action(call: str, state: _DebugState) -> dict | None:
    """debugTable action=definedAction: the fork picks the definition as a run would (keywords,
    number of arguments, loaded last wins) and cuts its block. None if this fork can't."""
    try:
        found = _debug_post(
            "debugTable", state.page or "", "definedAction", {"name": call, "session": state.session},
            timeout_seconds=30.0, state=state,
        )
    except ToolError as exc:
        if _unknown_action(exc):
            return None
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
    if not reload_result["reloaded"] and isinstance(result.get("reloadHtml"), str):
        reload_result["tables"] = _tables_text(result["reloadHtml"])
    if isinstance(result.get("reloadCounts"), dict) and not reload_result["reloaded"]:
        reload_result["counts"] = result["reloadCounts"]
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


def _log_line(line: dict) -> str:
    text = str(line.get("text", ""))
    if len(text) > _LOG_LINE_CHARS:
        text = f"{text[:_LOG_LINE_CHARS]}… ({len(text)} chars)"
    return f"{line.get('time', '')} [{line.get('stream', '')}] {text}"


def _log_lines(lines) -> list[str]:
    return [_log_line(l) for l in lines or [] if isinstance(l, dict)]


def _read_log(state: _DebugState, max_lines: int, timeout_seconds: float | None) -> dict:
    start = state.log_next
    data = _session_action(state, "log", {"from": start}, timeout_seconds=timeout_seconds)
    lines = data.get("lines") or []
    dropped = data.get("dropped") or 0
    more = len(lines) > max_lines
    # the first returned line has number start + dropped
    state.log_next = start + dropped + max_lines if more else data.get("next", start)
    return {"lines": _log_lines(lines[:max_lines]), "dropped": dropped, "more": more}


def _run_document(
    state: _DebugState, page: str | None, restart: bool, timeout_seconds: float | None, run
) -> dict:
    """Begin page's storytest if needed (nextPage), then run(); report the previous tear down."""
    teardown = None
    if (page and page != state.current_page) or restart:
        teardown = _next_page(state, page, timeout_seconds)
    result = run()
    if teardown is not None:
        result["previous_page_teardown"] = teardown
    return result


if DEBUG_TOOLSET:

    @mcp.tool(
        name="fitnesse_debug_start",
        description=(
            "Starts a debug session for stepwise testing on a KDTA FitNesse: a test process that "
            "stays alive between calls, so you can run tables one call at a time and the state (open "
            "browsers, objects in scope, variables) carries over from one call to the next. Use it to"
            " try out steps, build a new test, or step through a failing one; for a plain complete "
            "run use fitnesse_run_test instead. Nothing is written to the test history.\n"
            "The page decides which fixtures and variables are available, so start on the test page "
            "(or, with suite=true, on the suite page) you want to work on.\n"
            "Typical flow: fitnesse_debug_start -> fitnesse_debug_run_page(setup_only=true) -> "
            "fitnesse_debug_inspect(kind='keywords' or 'definedActions') -> fitnesse_debug_run_wiki or "
            "fitnesse_debug_run_page, repeatedly -> fitnesse_debug_end. Run the SetUp first: defined "
            "actions, the browser and often fixtures only exist once it has run.\n"
            "One session per page and FitNesse account (forks before mod #159: one per account). If "
            "one is already running on this page (possibly a person working in the browser), this "
            "fails and says so: then continue it with fitnesse_debug_status(adopt=true, session=...) "
            "or end it. Sessions on other pages are not affected. This server works with one session "
            "at a time: end its own before starting another. Use replace=true only when told to: it "
            "kills the running session without tear down, and a browser tab on it then shows 'replaced'.\n"
            "With suite=true the result contains 'pages': the suite's pages in run order, each with "
            "path, kind (test, suiteSetUp, suiteTearDown) and runnable (false = cannot run in this "
            "session; skip it).\n"
            "Always end the session with fitnesse_debug_end when done: it occupies a server slot and "
            "browsers until then."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_start(
        page_path: Annotated[str, Field(description="Dotted path of the test page, e.g. 'FrontPage.Project.MyTest'; with suite=true the suite page.")],
        suite: Annotated[bool, Field(description="Start on a suite page to run its test pages one after another; returns the list of pages.")] = False,
        replace: Annotated[bool, Field(description="Only when told to: kill a running session of this account on this page (older forks: anywhere; maybe a person's) without tear down.")] = False,
        suite_filter: Annotated[str | None, Field(description="suite=true: only tests matching any of these comma-separated tags.")] = None,
        run_tests_matching_all_tags: Annotated[str | None, Field(description="suite=true: only tests matching all of these comma-separated tags.")] = None,
        exclude_suite_filter: Annotated[str | None, Field(description="suite=true: leave out tests with any of these comma-separated tags.")] = None,
        first_test: Annotated[str | None, Field(description="suite=true: start the list at this test page.")] = None,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """status check, start and (suite) suitePages via responder=debugTest."""
        state = _debug_state()
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
                    f"{status.get('startedAt')}); this FitNesse allows one per account. Continue it with "
                    "fitnesse_debug_status(adopt=true), end it with fitnesse_debug_end, or pass "
                    "replace=true to kill it without tear down."
                )
            state.reset()
            data = _debug_post(
                "debugTest", page_path, "start", {"suite": True if suite else None},
                timeout_seconds=timeout_seconds, state=state,
            )
            state.session = str(data.get("session"))
            state.page = data.get("page") or page_path
            state.current_page = state.page
            state.suite = suite
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
            except ToolError as exc:
                data["pages_error"] = str(exc)
        return data

    @mcp.tool(
        name="fitnesse_debug_status",
        description=(
            "Shows the debug session of this FitNesse account and the new lines of the test process "
            "log since the last call. Answers immediately, even while another call is still running a"
            " long table, so use it to watch progress.\n"
            "Result: running (false = no session), page (where the session was started), currentPage "
            "(whose test is running), suite, busy, idleSecondsLeft (the session ends itself after "
            "this idle time), known_to_this_server, and log.lines ('time [out|err|info] text'; 'info'"
            " lines show the command, exit code and crashes). These describe this server's session, "
            "else the account's session on page_path, else its only one. sessions (mod #159) lists "
            "all running sessions of the account: session, page, currentPage, suite, busy, startedAt.\n"
            "busy=true means the process is still working on a call that timed out: wait and call "
            "this again until busy is false, or abort with fitnesse_debug_end(abort=true).\n"
            "known_to_this_server=false with running=true means someone else's session (or one from "
            "before a restart): the other debug tools refuse to touch it until you call this with "
            "adopt=true (with several sessions: plus session=<id> from 'sessions'). Only adopt a "
            "session you are meant to continue."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_debug_status(
        adopt: Annotated[bool, Field(description="Continue the running session of this account with this server's debug tools.")] = False,
        session: Annotated[str | None, Field(description="With adopt, when several sessions run: the id of the one to continue, from 'sessions'.")] = None,
        page_path: Annotated[str | None, Field(description="Rarely needed: page used for the permission check; defaults to the session's page, else FrontPage.")] = None,
        max_log_lines: Annotated[int, Field(description="Maximum number of new log lines to return; the rest follow on the next call. 0 for none.", ge=0)] = 50,
        timeout_seconds: float = _make_timeout(30.0),
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
                state.page = chosen.get("page")
                state.current_page = chosen.get("currentPage") or state.page
                state.suite = bool(chosen.get("suite"))
                known = True
        elif state.session and not known:
            state.reset()
        result = {**status, "known_to_this_server": known}
        if known and max_log_lines:
            result["log"] = _read_log(state, max_log_lines, timeout_seconds)
        return result

    @mcp.tool(
        name="fitnesse_debug_run_wiki",
        description=(
            "Runs FitNesse wiki tables in the debug session, e.g. '|with browser|'. Rows are "
            "'|cell|cell|' lines; separate tables with an empty line. Variables of the current page "
            "like ${BASE_URL} are resolved. The page's SetUp and TearDown are NOT added: run the SetUp "
            "first with fitnesse_debug_run_page(setup_only=true), otherwise the defined actions, the "
            "browser and fixtures it provides are missing.\n"
            "State carries over between calls: a browser opened in one call is still open in the "
            "next. All tables of one call run before the result comes back; it can take minutes (up "
            "to ~10).\n"
            "Result: 'tables' holds every table as text, one row per line; a cell's outcome is "
            "prefixed: [pass] check passed, [fail] check failed (the cell shows expected and actual),"
            " [error] exception (message and shortened stack trace), [ignore] not run. Cells without "
            "prefix are plain input. 'counts' are this call's right/wrong/ignores/exceptions; "
            "'page_totals' add up all calls since the current page's test began.\n"
            "Below each table come what belongs to it: folding areas after it ('after: <title>', "
            "e.g. FitLibrary's show after or logs), links ('link in row R, column C: text -> url') "
            "and images. Inline screenshots ('[image N]' in a cell) are attached as image content "
            f"(at most {DEBUG_MAX_IMAGES} per call); 'images' lists which one is where.\n"
            "Find the available keywords and their exact wording with "
            "fitnesse_debug_inspect(kind='keywords') before writing tables.\n"
            "restart=true first ends the running test (its tear down runs, result in "
            "previous_page_teardown) and begins it afresh: use it for a clean state."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_run_wiki(
        wiki: Annotated[str, Field(description="Wiki markup: one or more tables of '|cell|cell|' rows, tables separated by an empty line.")],
        restart: Annotated[bool, Field(description="First end the running test (tear down) and begin it afresh, for a clean state.")] = False,
        include_html: Annotated[bool, Field(description="Also return the raw result HTML (large; normally not needed).")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """(nextPage +) runWiki via responder=debugTest."""
        state = _require_session()
        with _document(state):
            return _with_images(_run_document(
                state, None, restart, timeout_seconds,
                lambda: _with_setup_hint(state, _document_result(
                    state,
                    _session_action(state, "runWiki", {"wiki": wiki}, timeout_seconds=timeout_seconds),
                    state.current_page, include_html,
                ), timeout_seconds),
            ))

    @mcp.tool(
        name="fitnesse_debug_page_tables",
        description=(
            "Lists the tables that running a page executes, in order, including those of its "
            "SetUp/TearDown and included pages: index, number of rows and the first row. Use the "
            "indices to run only some of them with fitnesse_debug_run_page(tables=[...]); setup_tables "
            "are those of the SetUp (what run_page(setup_only=true) runs). These indices "
            "differ from fitnesse_table_source's, which counts only the page's own tables: to edit a "
            "table, use its source_page and source_index with the fitnesse_table_* tools. A table of an"
            " included page (included=true, e.g. SetUp) is edited on that page, which changes it for "
            "every page that includes it. Requires a debug session; omit page_path for the page "
            "currently being tested."
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
        page = page_path or state.current_page or state.page
        tables = []
        own: dict[str, int] = {}
        parsed = _parse_tables(_page_html(state, page, timeout_seconds))
        for i, table in enumerate(parsed):
            first = " | ".join(_squash(c["text"], 80) for c in (table["rows"][0] if table["rows"] else []))
            source_page = table["include"] or page
            source_index = own[source_page] = own.get(source_page, -1) + 1
            entry = {"index": i, "rows": len(table["rows"]), "first_row": first,
                     "source_page": source_page, "source_index": source_index}
            if table["include"]:
                entry["included"] = True
            tables.append(entry)
        return {"page": page, "setup_tables": _setup_indices(parsed), "tables": tables}

    @mcp.tool(
        name="fitnesse_debug_run_page",
        description=(
            "Runs the tables of a test page in the debug session, exactly as a normal test run would "
            "send them (SetUp, TearDown and included pages included): all of them, or only the "
            "indices given in 'tables' (see fitnesse_debug_page_tables), in the order given. Result "
            "as for fitnesse_debug_run_wiki.\n"
            "If page_path is not the page currently being tested, the current page's test is ended "
            "first (its tear-down result comes back as previous_page_teardown) and the new page's "
            "test begins. Calling it again for the same page continues that page's test, so you can "
            "run its tables in several steps; restart=true begins it afresh.\n"
            "setup_only=true runs just the SetUp (the tables of the included SetUp page): do this "
            "right after fitnesse_debug_start, before fitnesse_debug_run_wiki or inspect. A later call "
            "without tables then leaves the SetUp out (skipped_setup lists its indices) instead of "
            "running it twice; restart=true or another page runs it again.\n"
            "only_failures=true returns only the tables with a [fail] or [error] cell, for long "
            "pages.\n"
            "In a suite session, call it for each page with runnable=true from fitnesse_debug_start's"
            " 'pages', in that order."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_run_page(
        page_path: DebugPath = None,
        tables: Annotated[list[int] | None, Field(description="Indices from fitnesse_debug_page_tables to run, in this order; omit to run the whole page.")] = None,
        restart: Annotated[bool, Field(description="Begin the page's test afresh even if it is already the current page.")] = False,
        setup_only: Annotated[bool, Field(description="Run only the SetUp tables (setup_tables of fitnesse_debug_page_tables). Not together with tables.")] = False,
        only_failures: Annotated[bool, Field(description="Return only the tables with a [fail] or [error] cell (counts as usual; tables_without_failures says how many were left out). For long pages where you look for what went wrong.")] = False,
        include_html: Annotated[bool, Field(description="Also return the raw result HTML (large; normally not needed).")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """(nextPage +) pageHtml + runTable via responder=debugTest."""
        if setup_only and tables is not None:
            raise ToolError("Pass either setup_only=true or tables, not both.")
        state = _require_session()
        page = page_path or state.current_page or state.page

        def run() -> dict:
            html = _page_html(state, page, timeout_seconds)
            chosen = tables
            skipped: list[int] = []
            if setup_only:
                chosen = _setup_indices(_parse_tables(html))
                if not chosen:
                    return {"page": page, "setup_tables": [], "hint": "The page has no SetUp tables; nothing was run."}
            elif tables is None and state.setup_ran == page:  # SetUp already ran in this test
                parsed = _parse_tables(html)
                skipped = _setup_indices(parsed)
                chosen = [i for i in range(len(parsed)) if i not in skipped]
                if not chosen:
                    return {"page": page, "skipped_setup": skipped, "hint": "Only SetUp tables, which already ran; nothing was run."}
            if chosen is not None:
                parts = _split_tables(html)
                bad = [i for i in chosen if not 0 <= i < len(parts)]
                if bad:
                    raise ToolError(f"Table indices {bad} out of range: the page has {len(parts)} tables.")
                html = "\n".join(parts[i] for i in chosen)
            data = _session_action(state, "runTable", {"html": html}, timeout_seconds=timeout_seconds)
            if setup_only:
                state.setup_ran = page
            if setup_only or tables is None:
                state.setup_seen = page
            result = _document_result(state, data, page, include_html, only_failures)
            if skipped:
                result["skipped_setup"] = skipped
            return result

        with _document(state):
            return _with_images(_run_document(state, page, restart, timeout_seconds, run))

    @mcp.tool(
        name="fitnesse_debug_inspect",
        description=(
            "Looks into the debug session. kind:\n"
            "- 'keywords': the keywords you can use in tables right now. Without filter: only the "
            "categories with their number of keywords (the full list is large). With filter (a word "
            "such as 'click', or a category such as 'Web / Browser'): matching entries in full: "
            "syntax, where %1, %2 are the cells for parameters (e.g. 'click %1' -> "
            "'|click|//button|'), parameters (label, allowed options), returnType, tableItems (the rows a keyword "
            "expects in the rest of its table; 'This statement is mandatory' in helpText marks "
            "required rows) and helpText. Defined actions are not in it: see 'definedActions'.\n"
            "- 'definedActions': the defined actions loaded in this session by |define actions at| "
            "(usually from SetUp): action (call syntax, parameters as cells), parameters, note, "
            "deprecated, author, page (where it is defined; edit it with "
            "fitnesse_debug_defined_action_source) and overrides. filter searches all fields. Empty "
            "until the page's 'define actions at' tables have run in the session.\n"
            "- 'dynamicVariables': variables set during the test (name, value); include_system adds "
            "the many unchanged Java system properties.\n"
            "- 'scope': the objects whose keywords are available, innermost first (level, name, "
            "class, depth).\n"
            "- 'liveView': the Selenium grid session of the open browser and live_view_url, a link "
            "where a person can watch the browser (password not included)."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_inspect(
        kind: Annotated[Literal["keywords", "definedActions", "dynamicVariables", "scope", "liveView"], Field(description="What to look at; see the tool description.")],
        filter: Annotated[str | None, Field(description="Only for kind='keywords' or 'definedActions': text to search for (case-insensitive), e.g. 'browser'.")] = None,
        limit: Annotated[int, Field(description="Only for kind='keywords' with filter: maximum number of entries.", ge=1)] = 30,
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
            return {"count": len(actions), "matches": len(hits), "definedActions": hits[:limit]}
        action = {"keywords": "keywordsJson"}.get(kind, kind)
        with _document(state):
            data = _session_action(state, action, timeout_seconds=timeout_seconds)
        html = data.get("html") or ""
        if kind == "keywords":
            return _keywords(html, filter, limit)
        if kind == "liveView":
            cells = (_query_rows(html) or [[]])[0]
            try:
                browsers = json.loads(cells[3]) if len(cells) > 3 and cells[3] else []
            except json.JSONDecodeError:
                browsers = cells[3]
            return _without_secrets({
                "hub": cells[1] if len(cells) > 1 else "",
                "selenium_session": cells[2] if len(cells) > 2 else "",
                "browsers": browsers,
                "live_view_url": data.get("liveViewUrl"),
                "live_view_only": data.get("liveViewOnly"),
            })
        records = _query_records(html, kind)
        if kind == "dynamicVariables":
            for record in records:
                record["system"] = record.get("system", "").lower() == "true"
            if not include_system:
                records = [r for r in records if not r["system"]]
        return {kind: records}

    @mcp.tool(
        name="fitnesse_debug_end",
        description=(
            "Ends the debug session: the running test's tear down (and, in a suite, the suite tear "
            "down) run, then the test process stops. Returns the tear-down result, the remaining log "
            "lines and page_totals (counts per page). Call it whenever you are done.\n"
            "abort=true stops the process immediately without tear down; it works even while another "
            "call is still running. Use it when the session hangs or busy stays true."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_end(
        abort: Annotated[bool, Field(description="Stop immediately without tear down, even while a call is running.")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """end/abort via responder=debugTest."""
        state = _require_session()
        form = {"logFrom": state.log_next}
        if abort:
            data = _session_action(state, "abort", form, timeout_seconds=30.0)
            result: dict = {"ended": data.get("ended", True), "aborted": True}
        else:
            with _document(state):
                data = _session_action(state, "end", form, timeout_seconds=timeout_seconds)
            result = {"ended": data.get("ended", True)}
            result["teardown"] = _document_result(state, data, state.current_page, include_html=False)
        log = data.get("log")
        if isinstance(log, dict):
            result["log"] = _log_lines(log.get("lines"))
        result["page_totals"] = state.page_counts
        state.reset()
        return _with_images(result)

    @mcp.tool(
        name="fitnesse_debug_defined_action_source",
        description=(
            "Reads the current wiki of a defined action for editing: finds the page that defines it"
            " via the session's defined actions, reads that page fresh (not what the session loaded)"
            " and cuts out the definition block as FitLibrary reads it: the header table (the page's"
            " first table or the first after a ---- line) and every table up to the next ----, with"
            " the text in between. Forks with definedAction pick the definition as a run would "
            "(keywords, number of arguments, loaded last wins). Result: page, index, last, original, "
            "ready for "
            "fitnesse_table_save(page_path=page, index=index, last=last, original=original, wiki=<new>,"
            " reload_defined_actions=true). On older forks it fails if the header is on the page "
            "more than once. "
            "Needs a debug session in which the action is loaded, or page_path, the defining page."
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
            found = _server_defined_action(_call_row(call), state)
            if found is not None:
                return found
            defined = defined or _session_defined_action(state, call, timeout_seconds)
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
            "Reads a wiki page as its tables and the text between them, for changing single tables "
            "with fitnesse_table_save, fitnesse_table_insert and fitnesse_table_delete. Result: "
            "tables (wiki markup of each of the page's own tables; tables from included pages and "
            "tables nested in cells are not counted) and texts, where texts[i] is the text before "
            "tables[i] and the last text follows the last table. Its indices differ from "
            "fitnesse_debug_page_tables', which also counts SetUp, TearDown and included pages. Does "
            "not need a debug session."
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
        " Saves the page for real (recent changes): change only pages you are asked to change. "
        "original is tables[index] or texts[index] from fitnesse_table_source, unchanged; if the "
        "page has changed since, this fails: read it again and redo the change. The result's source "
        "is the page after the change: take the next original and indices from it. A running debug "
        "session sees the change when the tables run again."
    )
    type TableIndex = Annotated[int, Field(description="Index among the page's own tables, as in fitnesse_table_source.", ge=0)]

    @mcp.tool(
        name="fitnesse_table_save",
        description=(
            "Replaces table index of a page with new wiki markup; with last, replaces tables index..last "
            "together with the text between them (e.g. a defined action spread over several tables).\n"
            "Editing a defined action: take page, index, last and original from "
            "fitnesse_debug_defined_action_source and pass reload_defined_actions=true. The running "
            "session then loads the page's defined actions again and runs the new version at once. "
            "The result has 'reload': reloaded (true = loaded), definedActions (the page's actions "
            "as now loaded), and if reloaded is false the reason, or error/code and the tables of the "
            "failed load. The page is saved either way: fix the definition and save again, or run "
            "fitnesse_debug_run_wiki('|define actions at|.<page>|') once the page is right."
        ) + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_table_save(
        page_path: WikiPath,
        index: TableIndex,
        original: Annotated[str, Field(description="Current wiki of table index (with last: of tables index..last and the text between), as from fitnesse_table_source.")],
        wiki: Annotated[str, Field(description="New wiki markup.")],
        last: Annotated[int | None, Field(description="Last table of a block to replace.", ge=0)] = None,
        reload_defined_actions: Annotated[bool, Field(description="After saving, reload the page's defined actions into the running debug session (|define actions at|.page|), so the session runs the new version.")] = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """save/saveBlock via responder=debugTable (+ runWiki |define actions at| via responder=debugTest)."""
        action = "save" if last is None else "saveBlock"
        return _save_and_reload(
            page_path, action, {"index": index, "last": last, "original": original, "wiki": wiki},
            timeout_seconds, reload_defined_actions,
        )

    @mcp.tool(
        name="fitnesse_table_insert",
        description="Inserts new table(s) before table index, into the text in front of it: at the end of that text, or with start=true at its beginning; index = number of tables appends them at the end of the page." + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_table_insert(
        page_path: WikiPath,
        index: TableIndex,
        before: Annotated[str, Field(description="Current wiki of table index (tables[index] of fitnesse_table_source); empty when appending at the end.")],
        wiki: Annotated[str, Field(description="Wiki markup of the new table(s).")],
        start: Annotated[bool, Field(description="Insert at the beginning of the text in front of table index instead of at its end.")] = False,
        reload_defined_actions: Annotated[bool, Field(description="After saving, reload the page's defined actions into the running debug session, e.g. after adding a new defined action.")] = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """insert via responder=debugTable."""
        return _save_and_reload(
            page_path, "insert", {"index": index, "before": before, "start": start, "wiki": wiki},
            timeout_seconds, reload_defined_actions,
        )

    @mcp.tool(
        name="fitnesse_table_move",
        description="Moves table index to position to (counted before the move) in one save: into the text in front of table to, at its end, or with start=true at its beginning; to = number of tables moves it to the end." + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_table_move(
        page_path: WikiPath,
        index: TableIndex,
        original: Annotated[str, Field(description="Current wiki of table index, as from fitnesse_table_source.")],
        to: Annotated[int, Field(description="Target position, counted before the move.", ge=0)],
        start: Annotated[bool, Field(description="Go to the beginning of the text in front of table to instead of its end.")] = False,
        reload_defined_actions: Annotated[bool, Field(description="After saving, reload the page's defined actions into the running debug session.")] = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """move via responder=debugTable."""
        return _save_and_reload(page_path, "move", {"index": index, "original": original, "to": to, "start": start}, timeout_seconds, reload_defined_actions)

    @mcp.tool(
        name="fitnesse_table_save_text",
        description="Replaces texts[index], the wiki text in front of table index (one more after the last table), e.g. headings, comments, collapsible sections. Refuses text that would remove or change a table; tables in the new text are added (result: added, their number)." + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_table_save_text(
        page_path: WikiPath,
        index: Annotated[int, Field(description="Index into texts of fitnesse_table_source.", ge=0)],
        original: Annotated[str, Field(description="Current texts[index], as from fitnesse_table_source.")],
        wiki: Annotated[str, Field(description="New wiki text.")],
        reload_defined_actions: Annotated[bool, Field(description="After saving, reload the page's defined actions into the running debug session.")] = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """saveText via responder=debugTable."""
        return _save_and_reload(page_path, "saveText", {"index": index, "original": original, "wiki": wiki},
                                timeout_seconds, reload_defined_actions)

    @mcp.tool(
        name="fitnesse_table_move_text",
        description="Moves texts[index] next to texts[to] in one save: after it, or with start=true in front of it." + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_table_move_text(
        page_path: WikiPath,
        index: Annotated[int, Field(description="Index into texts of the text to move.", ge=0)],
        original: Annotated[str, Field(description="Current texts[index].")],
        to: Annotated[int, Field(description="Index into texts of the target text.", ge=0)],
        to_original: Annotated[str, Field(description="Current texts[to].")],
        start: Annotated[bool, Field(description="Put it in front of texts[to] instead of after it.")] = False,
        reload_defined_actions: Annotated[bool, Field(description="After saving, reload the page's defined actions into the running debug session.")] = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """moveText via responder=debugTable."""
        return _save_and_reload(page_path, "moveText", {"index": index, "original": original, "to": to, "toOriginal": to_original, "start": start}, timeout_seconds, reload_defined_actions)

    @mcp.tool(
        name="fitnesse_table_delete",
        description="Removes table index from the page." + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_table_delete(
        page_path: WikiPath,
        index: TableIndex,
        original: Annotated[str, Field(description="Current wiki of table index, as from fitnesse_table_source.")],
        reload_defined_actions: Annotated[bool, Field(description="After saving, reload the page's defined actions into the running debug session.")] = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """delete via responder=debugTable."""
        return _save_and_reload(page_path, "delete", {"index": index, "original": original}, timeout_seconds, reload_defined_actions)


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


# Sent to the client at initialize; agents see it, the README they do not.
DEBUG_INSTRUCTIONS = """\
KDTA debug tools - how they depend on each other:
- Session first: fitnesse_debug_start (or fitnesse_debug_status(adopt=true) for a running \
session) must succeed before fitnesse_debug_run_wiki, _run_page, _page_tables, _inspect, \
_defined_action_source and _end work. fitnesse_debug_status without adopt and the fitnesse_table_* tools need no session.
- Typical flow: start -> run_page(setup_only=true) -> inspect(kind='keywords' / 'definedActions') \
-> run_wiki / run_page, repeatedly -> end. Run the SetUp first: defined actions, the browser and \
often fixtures come from it, so without it inspect shows no defined actions and run_wiki fails. \
Always call fitnesse_debug_end when done; the session holds server resources.
- Write run_wiki tables with the exact keyword wording from inspect(kind='keywords'). Defined \
actions are listed separately by inspect(kind='definedActions'), once the page's |define actions \
at| tables (usually in SetUp) have run in the session.
- State (open browser, scope, variables) carries over between run_wiki and run_page calls on \
the same page. run_page on another page, or restart=true, first ends the current page's test \
(tear down in previous_page_teardown).
- run_wiki adds no SetUp/TearDown: run_page(setup_only=true) runs the SetUp; other tables with \
run_page(tables=[...]), indices from fitnesse_debug_page_tables.
- Suite session (start with suite=true): call run_page for each page with runnable=true from \
'pages', in that order.
- busy=true in fitnesse_debug_status: wait and poll status, or fitnesse_debug_end(abort=true); \
a run_wiki/run_page call fails at once while busy.
"""

# fitnesse_table_source needs debug and read, like the base part.
DEBUG_TABLE_SOURCE_INSTRUCTIONS = """\
- Table indices differ: fitnesse_debug_page_tables counts every table a run executes (SetUp, \
TearDown, included pages); fitnesse_table_source counts only the page's own tables. Never pass \
an index from one to the other.
"""

# Only with write as well, where fitnesse_table_save/_insert/_delete are visible.
DEBUG_TABLE_EDIT_INSTRUCTIONS = """\
- Editing tables: read fitnesse_table_source, pass the changed table or text (tables[index] / \
texts[index]) unchanged as original to fitnesse_table_save / _insert / _delete / _move / \
_save_text / _move_text. Each result's source is the page after the change: take the next \
original and indices from it (indices shift). On "page changed" read fitnesse_table_source again \
and redo the change. A running session sees the change when the tables are run again.
- A table seen in a session: fitnesse_debug_page_tables gives its source_page and source_index; \
tables of included pages (SetUp, includes) are edited on that page.
- Editing a defined action: inspect(kind='definedActions') -> defined_action_source(action) -> \
fitnesse_table_save(page, index, last, original, wiki=<new>, reload_defined_actions=true) -> run \
it again. Check reload.reloaded in the result; false means the session still has the old version.
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
