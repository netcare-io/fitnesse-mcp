# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT

import json
import os
import uuid
from base64 import b64encode
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from urllib import parse, request
from urllib.error import HTTPError, URLError

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.transforms import Visibility
from pydantic import Field

mcp = FastMCP("Fitnesse MCP Server")

MAX_BYTES = int(
    os.getenv("FITNESSE_MAX_RESPONSE_BYTES", str(1024 * 1024))
)  # default 1 MB
MAX_UPLOAD_BYTES = int(
    os.getenv("FITNESSE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))
)  # default 10 MB

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
        description="Path under the FitNesse files section, e.g. 'files/images'. No leading slash."
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
    timeout_seconds: float = 30.0,
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
    timeout_seconds: float = 30.0,
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
            raw_bytes = response.read(MAX_UPLOAD_BYTES + 1)
            truncated = len(raw_bytes) > MAX_UPLOAD_BYTES
            return {
                "ok": True,
                "status": response.status,
                "url": url,
                "content_type": response.headers.get("Content-Type", ""),
                "truncated": truncated,
                "body": raw_bytes[:MAX_UPLOAD_BYTES],
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
        files_path,
        params={"responder": "createDir", "dirname": dirname},
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
        files_path,
        params={"responder": "deleteFile", "filename": filename},
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
            files_path,
            params={"responder": "files", "format": "json"},
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(
        name="fitnesse_download_file",
        description="Downloads a file from the files section directory identified by the resource and filename, saving it beneath FITNESSE_FILES_ROOT.",
        tags={"fitnesse", "rest", "files", "read"},
        annotations={"readOnlyHint": True},
    )
    def fitnesse_download_file(
        files_path: FilesPath,
        filename: str,
        local_filename: str | None = None,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """Download a file from the files section by GETing its direct resource path."""
        root = Path(os.environ["FITNESSE_FILES_ROOT"]).resolve()
        target = (root / (local_filename or filename)).resolve()
        # Reject paths that escape the files root.
        if not target.is_relative_to(root):
            return {"ok": False, "error": "Path is outside the configured files root"}

        result = _request_bytes(
            "GET",
            f"{files_path.rstrip('/')}/{filename}",
            timeout_seconds=timeout_seconds,
        )
        if not result["ok"]:
            return result
        if result["truncated"]:
            return {
                "ok": False,
                "error": f"File exceeds download limit of {MAX_UPLOAD_BYTES} bytes",
            }

        data = result.pop("body")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        result["local_path"] = str(target)
        result["bytes_written"] = len(data)
        return result


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
    timeout_seconds: float = _make_timeout(30.0),
) -> dict:
    """Get page history via responder=pageHistory."""
    params: dict[str, str | None] = {"responder": "pageHistory", "format": "xml"}
    if result_date:
        params["resultDate"] = result_date
    return _request("GET", page_path, params=params, timeout_seconds=timeout_seconds)


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
        files_path,
        params={"responder": "renameFile", "filename": filename, "newName": new_name},
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


if FITNESSE_COMPLETE_TOOLSET:

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


if FITNESSE_COMPLETE_TOOLSET:

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
    timeout_seconds: float = _make_timeout(120.0),
) -> dict:
    """Run a suite with full documented controls for responder=suite."""
    params: dict[str, str | None] = {"responder": "suite", "format": "xml"}
    if debug:
        params["debug"] = None
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
    timeout_seconds: float = _make_timeout(120.0),
) -> dict:
    """Run a test with responder=test and optional controls."""
    params: dict[str, str | None] = {"responder": "test", "format": "xml"}
    if debug:
        params["debug"] = None

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
        description="Uploads a file object into the files section directory selected by the resource path.",
        tags={"fitnesse", "rest", "files", "write"},
        annotations={"destructiveHint": False, "idempotentHint": True},
    )
    def fitnesse_upload_file(
        files_path: FilesPath,
        local_file_path: str,
        upload_filename: str | None = None,
        timeout_seconds: float = _make_timeout(60.0),
    ) -> dict:
        """Upload a file to files section via responder=upload and multipart form data."""
        root = Path(os.environ["FITNESSE_FILES_ROOT"]).resolve()
        target = (root / local_file_path).resolve()
        # Reject paths that escape the files root.
        if not target.is_relative_to(root):
            return {"ok": False, "error": "Path is outside the configured files root"}

        if not target.is_file():
            return {"ok": False, "error": f"File not found: {target.name!r}"}

        size = target.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            return {
                "ok": False,
                "error": f"File is {size} bytes, limit is {MAX_UPLOAD_BYTES}",
            }
        file_bytes = target.read_bytes()

        filename = upload_filename or target.name
        body, content_type = _build_multipart_form_data(
            fields={}, file_field="file", filename=filename, file_bytes=file_bytes
        )

        return _request(
            "POST",
            files_path,
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


if _env_flag("FITNESSE_READONLY"):
    # hide write/execute/control tools in read-only deployments
    mcp.add_transform(Visibility(False, tags={"write", "execute", "control"}))


if __name__ == "__main__":
    mcp.run()
