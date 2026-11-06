# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT

import binascii
import json
import logging
import mimetypes
import os
import threading
import uuid
import xml.etree.ElementTree as ET
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Annotated, Literal
from urllib import parse, request
from urllib.error import HTTPError, URLError

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.transforms import Visibility
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import File, Image
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
    Literal["Normal", "Test", "Suite", "Static", "Default"],
    Field(description="Page type."),
]


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Extra low-traffic/read-heavy tools trimmed from the default set; opt back in with FITNESSE_COMPLETE_TOOLSET=1.
FITNESSE_COMPLETE_TOOLSET = _env_flag("FITNESSE_COMPLETE_TOOLSET")


def _make_timeout(default: float):
    def factory() -> float:
        return default

    return Depends(factory)


def _make_run_timeout():
    def factory() -> float | None:
        return RUN_TIMEOUT_SECONDS

    return Depends(factory)


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
        base_url = os.getenv("FITNESSE_BASE_URL", "http://localhost:8080")
    if username is None:
        username = os.getenv("FITNESSE_USERNAME")
    if password is None:
        password = os.getenv("FITNESSE_PASSWORD")
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
        base_url = os.getenv("FITNESSE_BASE_URL", "http://localhost:8080")
    if username is None:
        username = os.getenv("FITNESSE_USERNAME")
    if password is None:
        password = os.getenv("FITNESSE_PASSWORD")
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
    description="Creates a new child page beneath the selected page using pageName and pageContent, with optional pageTemplate and pageType.",
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False},
)
def fitnesse_add_child_page(
    parent_page_path: WikiPath,
    page_name: PageName,
    page_content: PageContent = "",
    page_template: str | None = None,
    page_type: PageType | None = None,
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
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Delete a page via responder=deletePage."""
    params: dict[str, str | None] = {"responder": "deletePage", "confirmed": "yes"}
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


if FITNESSE_COMPLETE_TOOLSET:

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


if FITNESSE_COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_execute_search_properties",
        description="Returns pages matching property criteria such as pageType, Suites, Action, and exclude flags for setup, teardown, or obsolete pages.",
        tags={"fitnesse", "rest", "search", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_execute_search_properties(
        page_path: WikiPath,
        page_type: PageType | None = None,
        suites: str | None = None,
        action: str | None = None,
        exclude_setup: bool = False,
        exclude_teardown: bool = False,
        exclude_obsolete: bool = False,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """Search by properties via responder=executeSearchProperties."""
        params: dict[str, str | None] = {"responder": "executeSearchProperties"}
        if page_type:
            params["pageType"] = page_type
        if suites:
            params["Suites"] = suites
        if action:
            params["Action"] = action
        if exclude_setup:
            params["ExcludeSetUp"] = None
        if exclude_teardown:
            params["ExcludeTearDown"] = None
        if exclude_obsolete:
            params["ExcludeObsolete"] = None
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


if FITNESSE_COMPLETE_TOOLSET:

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


if FITNESSE_COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_instruction",
        description="Displays Slim instructions for a suite of Slim tests for analysis or low level debugging.",
        tags={"fitnesse", "rest", "tests", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_instruction(
        page_path: WikiPath,
        timeout_seconds: float = _make_timeout(120.0),
    ) -> dict:
        """Get Slim instructions via responder=instruction."""
        return _request(
            "GET",
            page_path,
            params={"responder": "instruction"},
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
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Move a page under another parent via responder=movePage."""
    return _request(
        "GET",
        page_path,
        params={"responder": "movePage", "newLocation": new_location},
        timeout_seconds=timeout_seconds,
    )


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


if FITNESSE_COMPLETE_TOOLSET:

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


if FITNESSE_COMPLETE_TOOLSET:

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


@mcp.tool(
    name="fitnesse_get_page_data",
    description="Returns the raw wiki text of the selected page via pageData responder.",
    tags={"fitnesse", "rest", "pages", "read"},
    annotations={"readOnlyHint": True},
)
def fitnesse_get_page_data(
    page_path: WikiPath,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Get raw wiki text via responder=pageData."""
    return _request(
        "GET",
        page_path,
        params={"responder": "pageData"},
        timeout_seconds=timeout_seconds,
    )


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


if FITNESSE_COMPLETE_TOOLSET:

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
    description="Purges old test history files while preserving the configured number of days.",
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": True},
)
def fitnesse_purge_history(
    days: int,
    page_path: WikiPath = "",
    timeout_seconds: float = _make_timeout(120.0),
) -> dict:
    """Purge old history via responder=purgeHistory."""
    return _request(
        "GET",
        page_path,
        params={"responder": "purgeHistory", "days": str(days)},
        timeout_seconds=timeout_seconds,
    )


if FITNESSE_COMPLETE_TOOLSET:

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


if FITNESSE_COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_refactor_screen",
        description="Displays the refactoring screen for the selected page.",
        tags={"fitnesse", "rest", "pages", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_refactor_screen(
        page_path: WikiPath,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get refactor screen via responder=refactor."""
        return _request(
            "GET",
            page_path,
            params={"responder": "refactor"},
            timeout_seconds=timeout_seconds,
        )


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
    new_name: str,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Rename a page using responder=renamePage."""
    return _request(
        "GET",
        page_path,
        params={"responder": "renamePage", "newName": new_name},
        timeout_seconds=timeout_seconds,
    )


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


@mcp.tool(
    name="fitnesse_save_page_content",
    description="Saves wiki page content via POST using pageContent and optional editTime, ticketId, and redirect fields.",
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_save_page_content(
    page_path: WikiPath,
    page_content: PageContent,
    edit_time: str | None = None,
    ticket_id: str | None = None,
    redirect: str | None = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Save a FitNesse page using responder=saveData form payload."""
    form_data: dict[str, str] = {"pageContent": page_content}
    if edit_time:
        form_data["editTime"] = edit_time
    if ticket_id:
        form_data["ticketId"] = ticket_id
    if redirect:
        form_data["redirect"] = redirect

    body = parse.urlencode(form_data)
    return _request(
        "POST",
        page_path,
        params={"responder": "saveData"},
        body=body,
        content_type="application/x-www-form-urlencoded",
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(
    name="fitnesse_save_properties",
    description="Saves page properties via POST including pageType, attribute flags, Suites tags, and HelpText.",
    tags={"fitnesse", "rest", "pages", "write"},
    annotations={"destructiveHint": False, "idempotentHint": True},
)
def fitnesse_save_properties(
    page_path: WikiPath,
    page_type: PageType = "Normal",
    suites: str | None = None,
    help_text: str | None = None,
    attributes: list[
        Literal[
            "Test",
            "Suite",
            "Prune",
            "Edit",
            "Versions",
            "Properties",
            "Refactor",
            "WhereUsed",
            "RecentChanges",
            "Files",
            "Search",
            "Secure-Test",
            "Secure-Edit",
            "Secure-Read",
        ]
    ]
    | None = None,
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Save properties with responder=saveProperties."""
    form_data: dict[str, str] = {"pageType": page_type}
    if suites is not None:
        form_data["Suites"] = suites
    if help_text is not None:
        form_data["HelpText"] = help_text
    if attributes:
        for attr in attributes:
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


if FITNESSE_COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_search",
        description="Searches for pages matching searchString with searchType for content or title matching.",
        tags={"fitnesse", "rest", "search", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_search(
        page_path: WikiPath,
        search_string: str,
        search_type: str = "content",
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Search pages with responder=search."""
        return _request(
            "GET",
            page_path,
            params={
                "responder": "search",
                "searchString": search_string,
                "searchType": search_type,
            },
            timeout_seconds=timeout_seconds,
        )

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


@mcp.tool(
    name="fitnesse_run_suite",
    description="Runs a suite of tests under the selected page with support for filters, debug options, and history controls. Returns XML.",
    tags={"fitnesse", "rest", "tests", "execute"},
    annotations={"destructiveHint": False},
)
def fitnesse_run_suite(
    page_path: WikiPath,
    debug: bool = False,
    remote_debug: bool = False,
    suite_filter: str | None = None,
    exclude_suite_filter: str | None = None,
    first_test: str | None = None,
    nohistory: bool = False,
    includehtml: bool = False,
    nochunk: bool = False,
    variables: dict[str, str] | None = None,
    timeout_seconds: float | None = _make_run_timeout(),
) -> dict:
    """Run a suite with full documented controls for responder=suite."""
    params: dict[str, str | None] = {"responder": "suite", "format": "xml"}
    if debug:
        params["debug"] = None
    if remote_debug:
        params["remote_debug"] = None
    if suite_filter:
        params["suiteFilter"] = suite_filter
    if exclude_suite_filter:
        params["excludeSuiteFilter"] = exclude_suite_filter
    if first_test:
        params["firstTest"] = first_test
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
    description="Runs a test page and supports debug, remote_debug, nochunk, and markup variable inputs. Returns XML.",
    tags={"fitnesse", "rest", "tests", "execute"},
    annotations={"destructiveHint": False},
)
def fitnesse_run_test(
    page_path: WikiPath,
    debug: bool = False,
    remote_debug: bool = False,
    nochunk: bool = False,
    variables: dict[str, str] | None = None,
    timeout_seconds: float | None = _make_run_timeout(),
) -> dict:
    """Run a test with responder=test and optional controls."""
    params: dict[str, str | None] = {"responder": "test", "format": "xml"}
    if debug:
        params["debug"] = None
    if remote_debug:
        params["remote_debug"] = None
    if nochunk:
        params["nochunk"] = None
    params = _merge_extra(params, variables)
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


if FITNESSE_COMPLETE_TOOLSET:

    @mcp.tool(
        name="fitnesse_get_test_history",
        description="Displays current test history for a page subtree or all history. Returns XML.",
        tags={"fitnesse", "rest", "history", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_get_test_history(
        page_path: WikiPath = "",
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """Get test history via responder=testHistory."""
        return _request(
            "GET",
            page_path,
            params={"responder": "testHistory", "format": "xml"},
            timeout_seconds=timeout_seconds,
        )


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
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """View one saved version via responder=viewVersion."""
    return _request(
        "GET",
        page_path,
        params={"responder": "viewVersion", "version": version},
        timeout_seconds=timeout_seconds,
    )


if FITNESSE_COMPLETE_TOOLSET:

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


if FITNESSE_COMPLETE_TOOLSET:

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
# KDTA debug mode: responder=debugTest and responder=debugTable.
# These responders exist only in netcare's FitNesse fork (fitnesse-netcare-mod),
# so the tools are registered only with FITNESSE_KDTA_DEBUG set.
# ---------------------------------------------------------------------------

FITNESSE_KDTA_DEBUG = _env_flag("FITNESSE_KDTA_DEBUG")
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
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        self.session = self.page = self.current_page = None
        self.suite = False
        self.log_next = 0
        self.page_counts = {}


_DEBUG_STATES: dict[tuple[str, str], _DebugState] = {}
_DEBUG_STATES_LOCK = threading.Lock()


def _debug_state() -> _DebugState:
    key = (
        os.getenv("FITNESSE_BASE_URL", "http://localhost:8080").rstrip("/"),
        os.getenv("FITNESSE_USERNAME") or "",
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
    429: "Too many debug sessions on this FitNesse (debugTest.maxSessions). Try again later.",
    500: "If the session crashed, read the log via fitnesse_debug_status and start a new session.",
    504: "The document did not finish within debugTest.timeoutMinutes; the process keeps working on it. Poll fitnesse_debug_status (shows the log) until busy is false, or call fitnesse_debug_end with abort=true.",
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
    base_url = os.getenv("FITNESSE_BASE_URL", "http://localhost:8080")
    username = os.getenv("FITNESSE_USERNAME")
    password = os.getenv("FITNESSE_PASSWORD")
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
            "FITNESSE_USERNAME/FITNESSE_PASSWORD; Basic Auth bypasses the SSO), or this FitNesse "
            f"has no {responder} responder."
        )
    if status >= 400:
        code = data.get("code")
        message = data.get("error") or f"HTTP {status}"
        hint = _DEBUG_HINTS.get(status, "")
        if status == 409 and code in {"noSession", "replaced"} and state is not None:
            state.reset()
            hint = (
                "The debug session was replaced, e.g. from a browser with the same account. "
                "This MCP server forgot it; start a new one with fitnesse_debug_start."
                if code == "replaced"
                else "No debug session is running; start one with fitnesse_debug_start."
            )
        elif status == 409 and responder == "debugTable":
            hint = "The page changed since 'original' was read. Read fitnesse_table_source again and retry."
        detail = f" ({code})" if code else ""
        raise ToolError(f"HTTP {status}{detail}: {message}. {hint}".strip())
    return _without_secrets(data)


# ---- HTML tables ----------------------------------------------------------

_CELL_STATES = ("pass", "fail", "error", "ignore")


class _TableParser(HTMLParser):
    """Collect the top-level <table> elements of an HTML document.

    Per table: its source span [start, end) and its rows of cells, each cell
    with its text (nested tables flattened into it) and FitLibrary status.
    """

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self._source = source
        self._line_starts = [0]
        for i, ch in enumerate(source):
            if ch == "\n":
                self._line_starts.append(i + 1)
        self.tables: list[dict] = []
        self._depth = 0
        self._cell: dict | None = None
        self._skip = 0  # inside <script>/<style>
        self.feed(source)
        self.close()

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _text(self, text: str) -> None:
        if self._cell is not None:
            self._cell["parts"].append(text)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append({"start": self._offset(), "end": None, "rows": []})
            else:
                self._text("\n")
        elif tag == "tr":
            if self._depth == 1:
                self.tables[-1]["rows"].append([])
            else:
                self._text("\n")
        elif tag in {"td", "th"}:
            if self._depth == 1 and self.tables and self.tables[-1]["rows"]:
                classes = (dict(attrs).get("class") or "").split()
                status = next((c for c in classes if c in _CELL_STATES), None)
                self._cell = {"parts": [], "status": status}
                self.tables[-1]["rows"][-1].append(self._cell)
            else:
                self._text(" | ")
        elif tag in {"br", "p", "div", "li", "pre"}:
            self._text("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._text("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
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

    def handle_data(self, data):
        if not self._skip:
            self._text(data)


def _parse_tables(html: str) -> list[dict]:
    tables = _TableParser(html or "").tables
    for table in tables:
        if table["end"] is None:  # unclosed
            table["end"] = len(html)
        table["rows"] = [
            [{"text": "".join(c["parts"]), "status": c["status"]} for c in row]
            for row in table["rows"]
        ]
    return tables


def _squash(text: str, limit: int | None = None) -> str:
    lines = [" ".join(line.split()) for line in text.strip().splitlines()]
    text = "\n".join(line for line in lines if line)
    limit = DEBUG_MAX_CELL_CHARS if limit is None else limit
    if limit > 0 and len(text) > limit:
        text = f"{text[:limit]}… [{len(text) - limit} chars cut]"
    return text


def _tables_text(html: str) -> str:
    """Condense annotated FitLibrary HTML into one text block per table.

    Cells carry their status as a prefix: [pass], [fail], [error], [ignore].
    """
    blocks = []
    for i, table in enumerate(_parse_tables(html)):
        lines = [f"Table {i}:"]
        for row in table["rows"]:
            cells = []
            for cell in row:
                text = _squash(cell["text"]).replace("\n", " ⏎ ")
                cells.append(f"[{cell['status']}] {text}" if cell["status"] else text)
            lines.append("| " + " | ".join(cells) + " |")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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


def _document_result(state: _DebugState, data: dict, page: str | None, include_html: bool) -> dict:
    html = data.get("html") or ""
    result = {
        "page": page,
        "counts": data.get("counts"),
        "page_totals": _add_counts(state, page, data.get("counts")),
        "tables": _tables_text(html),
    }
    if include_html:
        result["html"] = html
    return result


def _next_page(state: _DebugState, page: str | None, timeout_seconds: float | None) -> dict:
    previous = state.current_page
    data = _session_action(state, "nextPage", {"page": page}, timeout_seconds=timeout_seconds)
    teardown = _document_result(state, data, previous, include_html=False)
    teardown["teardown_of"] = teardown.pop("page")
    state.current_page = data.get("page") or page or previous
    state.page_counts.pop(state.current_page, None)
    return teardown


def _status(
    page_path: str, state: _DebugState, timeout_seconds: float | None, with_session: bool = True
) -> dict:
    form = {"session": state.session} if state.session and with_session else None
    return _debug_post("debugTest", page_path, "status", form, timeout_seconds=timeout_seconds, state=state)


def _split_tables(html: str) -> list[str]:
    return [html[t["start"]:t["end"]] for t in _parse_tables(html)]


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
    categories: dict[str, list[str]] = {}
    for entry in found:
        label = entry["category"] + (" (special)" if entry["special"] else "")
        categories.setdefault(label, []).append(entry.get("syntax"))
    return {"count": len(found), "categories": categories}


def _log_lines(lines) -> list[str]:
    return [f"{l.get('time', '')} [{l.get('stream', '')}] {l.get('text', '')}" for l in lines or []]


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


if FITNESSE_KDTA_DEBUG:

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
            "Typical flow: fitnesse_debug_start -> fitnesse_debug_inspect(kind='keywords') -> "
            "fitnesse_debug_run_wiki or fitnesse_debug_run_page, repeatedly -> fitnesse_debug_end.\n"
            "Only one session per FitNesse account. If one is already running (possibly a person "
            "working in the browser), this fails and says so: then continue it with "
            "fitnesse_debug_status(adopt=true) or end it with fitnesse_debug_end. Use replace=true "
            "only when told to: it kills the running session without tear down.\n"
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
        replace: Annotated[bool, Field(description="Only when told to: kill a running session of this account (maybe a person's) without tear down.")] = False,
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
            if status.get("running") and not replace:
                owner = "this MCP server" if state.session and str(status.get("session")) == state.session else "someone using the same account"
                raise ToolError(
                    f"A debug session started by {owner} is already running (page "
                    f"{status.get('page')}, current page {status.get('currentPage')}, started "
                    f"{status.get('startedAt')}). Continue it with fitnesse_debug_status(adopt=true), "
                    "end it with fitnesse_debug_end, or pass replace=true to kill it without tear down."
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
            " lines show the command, exit code and crashes).\n"
            "busy=true means the process is still working on a call that timed out: wait and call "
            "this again until busy is false, or abort with fitnesse_debug_end(abort=true).\n"
            "known_to_this_server=false with running=true means someone else's session (or one from "
            "before a restart): the other debug tools refuse to touch it until you call this with "
            "adopt=true. Only adopt a session you are meant to continue."
        ),
        tags={"fitnesse", "debug", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_debug_status(
        adopt: Annotated[bool, Field(description="Continue the running session of this account with this server's debug tools.")] = False,
        page_path: Annotated[str | None, Field(description="Rarely needed: page used for the permission check; defaults to the session's page, else FrontPage.")] = None,
        max_log_lines: Annotated[int, Field(description="Maximum number of new log lines to return; the rest follow on the next call. 0 for none.", ge=0)] = 50,
        timeout_seconds: float = _make_timeout(30.0),
    ) -> dict:
        """status (and log) via responder=debugTest."""
        state = _debug_state()
        status = _status(page_path or state.page or "FrontPage", state, timeout_seconds, with_session=False)
        running = bool(status.get("running"))
        known = running and bool(state.session) and str(status.get("session")) == state.session
        if adopt and running and not known:
            state.reset()
            state.session = str(status.get("session"))
            state.page = status.get("page")
            state.current_page = status.get("currentPage") or state.page
            state.suite = bool(status.get("suite"))
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
            "like ${BASE_URL} are resolved. The page's SetUp and TearDown are NOT added: run them "
            "yourself with fitnesse_debug_run_page if the steps need them.\n"
            "State carries over between calls: a browser opened in one call is still open in the "
            "next. All tables of one call run before the result comes back; it can take minutes (up "
            "to ~10).\n"
            "Result: 'tables' holds every table as text, one row per line; a cell's outcome is "
            "prefixed: [pass] check passed, [fail] check failed (the cell shows expected and actual),"
            " [error] exception (message and shortened stack trace), [ignore] not run. Cells without "
            "prefix are plain input. 'counts' are this call's right/wrong/ignores/exceptions; "
            "'page_totals' add up all calls since the current page's test began.\n"
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
            return _run_document(
                state, None, restart, timeout_seconds,
                lambda: _document_result(
                    state,
                    _session_action(state, "runWiki", {"wiki": wiki}, timeout_seconds=timeout_seconds),
                    state.current_page, include_html,
                ),
            )

    @mcp.tool(
        name="fitnesse_debug_page_tables",
        description=(
            "Lists the tables that running a page executes, in order, including those of its "
            "SetUp/TearDown and included pages: index, number of rows and the first row. Use the "
            "indices to run only some of them with fitnesse_debug_run_page(tables=[...]). Requires a "
            "debug session; omit page_path for the page currently being tested."
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
        for i, table in enumerate(_parse_tables(_page_html(state, page, timeout_seconds))):
            first = " | ".join(_squash(c["text"], 80) for c in (table["rows"][0] if table["rows"] else []))
            tables.append({"index": i, "rows": len(table["rows"]), "first_row": first})
        return {"page": page, "tables": tables}

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
        include_html: Annotated[bool, Field(description="Also return the raw result HTML (large; normally not needed).")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """(nextPage +) pageHtml + runTable via responder=debugTest."""
        state = _require_session()
        page = page_path or state.current_page or state.page

        def run() -> dict:
            html = _page_html(state, page, timeout_seconds)
            if tables is not None:
                parts = _split_tables(html)
                bad = [i for i in tables if not 0 <= i < len(parts)]
                if bad:
                    raise ToolError(f"Table indices {bad} out of range: the page has {len(parts)} tables.")
                html = "\n".join(parts[i] for i in tables)
            data = _session_action(state, "runTable", {"html": html}, timeout_seconds=timeout_seconds)
            return _document_result(state, data, page, include_html)

        with _document(state):
            return _run_document(state, page, restart, timeout_seconds, run)

    @mcp.tool(
        name="fitnesse_debug_inspect",
        description=(
            "Looks into the debug session. kind:\n"
            "- 'keywords': the keywords you can use in tables right now. Without filter: per category"
            " the keyword syntax, where %1, %2 are the cells for parameters (e.g. 'click %1' -> "
            "'|click|//button|'). With filter (e.g. 'browser', 'click'): matching entries in full, "
            "with parameters (label, allowed options), returnType, tableItems (the rows a keyword "
            "expects in the rest of its table; 'This statement is mandatory' in helpText marks "
            "required rows) and helpText.\n"
            "- 'dynamicVariables': variables set during the test (name, value); include_system adds "
            "the many unchanged Java system properties.\n"
            "- 'scope': the objects whose keywords are available, innermost first (level, name, "
            "class, depth).\n"
            "- 'liveView': the Selenium grid session of the open browser and live_view_url, a link "
            "where a person can watch the browser (password not included)."
        ),
        tags={"fitnesse", "debug", "execute"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_debug_inspect(
        kind: Annotated[Literal["keywords", "dynamicVariables", "scope", "liveView"], Field(description="What to look at; see the tool description.")],
        filter: Annotated[str | None, Field(description="Only for kind='keywords': text to search for (case-insensitive), e.g. 'browser'.")] = None,
        limit: Annotated[int, Field(description="Only for kind='keywords' with filter: maximum number of entries.", ge=1)] = 30,
        include_system: Annotated[bool, Field(description="Only for kind='dynamicVariables': also list the unchanged Java system properties (many).")] = False,
        timeout_seconds: float | None = Depends(lambda: DEBUG_TIMEOUT_SECONDS),
    ) -> dict:
        """keywordsJson/dynamicVariables/scope/liveView via responder=debugTest."""
        state = _require_session()
        action = "keywordsJson" if kind == "keywords" else kind
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
        return result

    @mcp.tool(
        name="fitnesse_table_source",
        description=(
            "Reads a wiki page as its tables and the text between them, for changing single tables "
            "with fitnesse_table_save, fitnesse_table_insert and fitnesse_table_delete. Result: "
            "tables (wiki markup of each of the page's own tables; tables from included pages and "
            "tables nested in cells are not counted) and texts, where texts[i] is the text before "
            "tables[i] and the last text follows the last table. Does not need a debug session."
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

    # debugTable saves wiki pages for real: tagged write, so FITNESSE_READONLY hides them.
    _TABLE_EDIT = (
        " This saves the wiki page for real (it shows up in recent changes): change only pages you "
        "are asked to change. Read fitnesse_table_source first and pass its texts unchanged; if the "
        "page has changed since, the call fails: read it again and redo the change. Indices refer "
        "to the source as read, so after a change read it again before the next one. A running "
        "debug session sees the change when the page's tables are run again."
    )
    type TableIndex = Annotated[int, Field(description="Index among the page's own tables, as in fitnesse_table_source.", ge=0)]

    @mcp.tool(
        name="fitnesse_table_save",
        description="Replaces table index of a page with new wiki markup; with last, replaces tables index..last together with the text between them (e.g. a defined action spread over several tables)." + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": True},
    )
    def fitnesse_table_save(
        page_path: WikiPath,
        index: TableIndex,
        original: Annotated[str, Field(description="Current wiki of table index (with last: of tables index..last and the text between), as from fitnesse_table_source.")],
        wiki: Annotated[str, Field(description="New wiki markup.")],
        last: Annotated[int | None, Field(description="Last table of a block to replace.", ge=0)] = None,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """save/saveBlock via responder=debugTable."""
        action = "save" if last is None else "saveBlock"
        return _debug_post(
            "debugTable", page_path, action,
            {"index": index, "last": last, "original": original, "wiki": wiki},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(
        name="fitnesse_table_insert",
        description="Inserts new table(s) before table index; index = number of tables appends them at the end of the page." + _TABLE_EDIT,
        tags={"fitnesse", "debug", "write"},
        annotations={"destructiveHint": False},
    )
    def fitnesse_table_insert(
        page_path: WikiPath,
        index: TableIndex,
        before: Annotated[str, Field(description="Current text preceding table index (texts[index] of fitnesse_table_source).")],
        wiki: Annotated[str, Field(description="Wiki markup of the new table(s).")],
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """insert via responder=debugTable."""
        return _debug_post(
            "debugTable", page_path, "insert",
            {"index": index, "before": before, "wiki": wiki},
            timeout_seconds=timeout_seconds,
        )

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
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """delete via responder=debugTable."""
        return _debug_post(
            "debugTable", page_path, "delete",
            {"index": index, "original": original},
            timeout_seconds=timeout_seconds,
        )


if _env_flag("FITNESSE_READONLY"):
    # hide write/execute/control tools in read-only deployments
    mcp.add_transform(Visibility(False, tags={"write", "execute", "control"}))


if __name__ == "__main__":
    mcp.run()
