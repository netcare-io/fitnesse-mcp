# fitnesse-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![FastMCP](https://img.shields.io/badge/FastMCP-4.0.0b2-blue.svg)](https://gofastmcp.com)

An MCP server that exposes [FitNesse](http://fitnesse.org/)'s REST responders as
tools, letting an MCP client read wiki pages, run tests and suites, manage the
files section, and inspect test history on a FitNesse instance.

Copyright (c) 2026 [netcare GmbH](https://github.com/netcare-io). Released under
the [MIT License](LICENSE).

---

## Requirements

- [VS Code](https://code.visualstudio.com/) with the
  [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers),
  plus Docker — the devcontainer supplies the Python toolchain and dependencies
- A reachable FitNesse instance

Working outside the devcontainer? You'll need Python 3.12+ and FastMCP 4, which
is **currently a prerelease** and must be pinned exactly:

```bash
pip install "fastmcp==4.0.0b2"
```

Using uv? `fastmcp` is a thin wrapper that depends on `fastmcp-slim` at the same
version, and uv only allows prereleases for packages you name explicitly:

```toml
[project]
dependencies = ["fastmcp==4.0.0b2"]

[tool.uv]
constraint-dependencies = ["fastmcp-slim==4.0.0b2"]
```

> **Pin exactly, not `>=4.0.0b1`.** Each beta in the v4 line has carried
> breaking changes. This project tracks the prerelease and the pin will move at
> GA.

---

## Quickstart

**1. Open the project in its devcontainer.**

```bash
git clone https://github.com/netcare-io/fitnesse-mcp.git
cd fitnesse-mcp
code .
```

VS Code detects `.devcontainer/` and prompts to reopen in the container — accept
it, or run **Dev Containers: Reopen in Container** from the command palette
(`F1`). The first build takes a few minutes; later starts are quick. Python and
all dependencies are installed inside the container, so there's nothing to set
up on your host.

**2. Point the server at your FitNesse instance.**

Run the remaining commands in the container's terminal:

```bash
export FITNESSE_BASE_URL=http://your-fitnesse-host:8080
export FITNESSE_READONLY=1          # recommended for a first run

fastmcp run server.py
```

`localhost` in that URL refers to the container, not your host — see
[Troubleshooting](#troubleshooting) if the connection is refused.

---

## Security

This server gives an LLM client the ability to **delete pages, purge test
history, and roll back versions** on your FitNesse instance. Two controls limit
that, and both are opt-in:

- **Start with `FITNESSE_READONLY=1`.** This hides every write, execute, and
  control tool, leaving the 23 read-only tools. Open it up deliberately once you
  know which operations you actually want the model to perform.
- **`fitnesse_shutdown` is not registered at all** unless
  `FITNESSE_ALLOW_SHUTDOWN` is set. It stops the FitNesse server.

Two further notes:

- Uploads are disabled unless `FITNESSE_UPLOAD_ROOT` is set, and
  `fitnesse_upload_file` will only read files resolving inside that directory.
- Credentials in `claude_desktop_config.json` are stored in **cleartext**. For
  shared machines, prefer the HTTP pattern below with the credentials in the
  server's own environment.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `FITNESSE_BASE_URL` | `http://localhost:8080` | FitNesse server base URL |
| `FITNESSE_USERNAME` | _(none)_ | Basic Auth username |
| `FITNESSE_PASSWORD` | _(none)_ | Basic Auth password |
| `FITNESSE_READONLY` | `false` | `1`/`true`/`yes`/`on` hides all write, execute, and control tools |
| `FITNESSE_ALLOW_SHUTDOWN` | `false` | `1`/`true`/`yes`/`on` exposes `fitnesse_shutdown` |
| `FITNESSE_UPLOAD_ROOT` | _(none — uploads disabled)_ | Absolute host path; `fitnesse_upload_file` only reads files beneath it |
| `FITNESSE_MAX_RESPONSE_BYTES` | `1048576` (1 MB) | Responses above this are truncated and flagged `"truncated": true` |
| `FITNESSE_MAX_UPLOAD_BYTES` | `10485760` (10 MB) | Uploads above this are rejected |

---

## Tools

42 tools by default (43 with `FITNESSE_ALLOW_SHUTDOWN`), each mapping to one
FitNesse responder. Under `FITNESSE_READONLY`, only the 23 **read** tools are
exposed.

<details>
<summary><b>Full tool list</b></summary>

### Pages — read

| Tool | Responder |
|---|---|
| `fitnesse_get_page` | `getPage` |
| `fitnesse_get_raw` | `raw` |
| `fitnesse_get_page_data` | `pageData` |
| `fitnesse_get_packet` | `packet` — all tables on a page, as JSON |
| `fitnesse_get_properties` | `properties` |
| `fitnesse_get_variables` | `variables` |
| `fitnesse_list_names` | `names` |
| `fitnesse_edit_page` | `edit` — with redirect/nonExistent options |
| `fitnesse_get_page_content` | `edit` — convenience alias |
| `fitnesse_get_new_page_form` | `new` |
| `fitnesse_get_refactor_screen` | `refactor` |
| `fitnesse_get_rss` | `rss` |

### Pages — write

| Tool | Responder |
|---|---|
| `fitnesse_add_child_page` | `addChild` |
| `fitnesse_save_page_content` | `saveData` (POST form data) |
| `fitnesse_save_properties` | `saveProperties` (POST form data) |
| `fitnesse_rename_page` | `renamePage` |
| `fitnesse_move_page` | `movePage` |
| `fitnesse_delete_page` | `deletePage` |
| `fitnesse_manage_symlink` | `symlink` |
| `fitnesse_import_pages` | `import` |
| `fitnesse_import_and_view` | `importAndView` |
| `fitnesse_publish` | `publish` |

### Tests

| Tool | Responder | Mode |
|---|---|---|
| `fitnesse_run_test` | `test` | execute |
| `fitnesse_run_suite` | `suite` — suite filters, debug, nochunk | execute |
| `fitnesse_get_instruction` | `instruction` — Slim instructions | read |
| `fitnesse_stop_test` | `stoptest` | control |
| `fitnesse_shutdown` | `shutdown` _(needs `FITNESSE_ALLOW_SHUTDOWN`)_ | control |

### History & versions

| Tool | Responder | Mode |
|---|---|---|
| `fitnesse_get_test_history` | `testHistory` | read |
| `fitnesse_get_page_history` | `pageHistory` | read |
| `fitnesse_compare_history` | `compareHistory` | read |
| `fitnesse_get_versions` | `versions` | read |
| `fitnesse_view_version` | `viewVersion` | read |
| `fitnesse_rollback_version` | `rollback` | write |
| `fitnesse_purge_history` | `purgeHistory` | write |

### Search

| Tool | Responder |
|---|---|
| `fitnesse_search` | `search` — read |
| `fitnesse_execute_search_properties` | `executeSearchProperties` — read |
| `fitnesse_get_search_form` | `searchForm` — read |
| `fitnesse_where_used` | `whereUsed` — read |

### Files section

| Tool | Responder | Mode |
|---|---|---|
| `fitnesse_list_files` | `files` | read |
| `fitnesse_create_dir` | `createDir` | write |
| `fitnesse_upload_file` | `upload` (POST multipart) | write |
| `fitnesse_rename_file` | `renameFile` | write |
| `fitnesse_delete_file` | `deleteFile` | write |

</details>

Flag-style FitNesse inputs are supported by passing `None` as a query param
value — for example `{"nohistory": None}` produces `?nohistory`.

---

## Connecting an MCP client

### Pattern 1 — stdio (simple, local)

The client launches the server as a subprocess and talks over stdin/stdout. No
server process to manage.

```json
{
  "mcpServers": {
    "fitnesse": {
      "command": "fastmcp",
      "args": ["run", "server.py"],
      "env": {
        "FITNESSE_BASE_URL": "http://your-fitnesse-host:8080",
        "FITNESSE_USERNAME": "your-username",
        "FITNESSE_PASSWORD": "your-password",
        "FITNESSE_READONLY": "1"
      }
    }
  }
}
```

Running the server from a Docker image instead? `-i` keeps stdin open, and each
`-e` forwards one variable from the `env` block into the container. **Every
variable you set in `env` needs its own `-e` flag** — anything missing here is
silently ignored inside the container:

```json
{
  "mcpServers": {
    "fitnesse": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "FITNESSE_BASE_URL",
        "-e", "FITNESSE_USERNAME",
        "-e", "FITNESSE_PASSWORD",
        "-e", "FITNESSE_READONLY",
        "fitnesse-mcp:latest"
      ],
      "env": {
        "FITNESSE_BASE_URL": "http://your-fitnesse-host:8080",
        "FITNESSE_USERNAME": "your-username",
        "FITNESSE_PASSWORD": "your-password",
        "FITNESSE_READONLY": "1"
      }
    }
  }
}
```

### Pattern 2 — HTTP (shared, production)

Use this when several clients share one server instance, or when the server runs
in Docker. Credentials live in the server's environment rather than in each
client's config.

```bash
FITNESSE_BASE_URL=http://your-fitnesse-host:8080 \
FITNESSE_USERNAME=your-username \
FITNESSE_PASSWORD=your-password \
fastmcp run server.py --transport http
```

Listens on port 8000 by default; override with `--port`.

Most clients can point at the URL directly:

```json
{
  "mcpServers": {
    "fitnesse": { "url": "http://localhost:8000/mcp" }
  }
}
```

For clients that only speak stdio, `fastmcp run <url>` proxies to it (internally).

---

## Interactive testing

Create fastmcp.json file.

```bash
./scripts/run-inspector.sh
```

Open the exact URL printed in the terminal — it carries a
`?MCP_INSPECTOR_API_TOKEN=...` query parameter.


---

## Troubleshooting

**`401 Unauthorized`** — either `FITNESSE_USERNAME`/`FITNESSE_PASSWORD` are
wrong, or FitNesse isn't configured for authentication and is rejecting the
header. Confirm with `curl -u user:pass "$FITNESSE_BASE_URL/FrontPage?responder=raw"`.

**`Connection refused`** — check `FITNESSE_BASE_URL`. Inside a container,
`localhost` is the container, not your host; use `host.docker.internal` (Docker
Desktop) or the host's LAN address.

**A write tool is missing** — `FITNESSE_READONLY` is set. Note that any value
other than `1`/`true`/`yes`/`on` counts as unset.

**`"truncated": true` in a response** — the body exceeded
`FITNESSE_MAX_RESPONSE_BYTES` and was cut. Common on suite runs with
`includehtml`. Raise the limit or narrow the request.

**`Invalid path`** — the page path contained `?`, `#`, `..`, or a null byte.
FitNesse page paths are dotted (`FrontPage.MySuite.MyTest`) with no leading
slash.

**`ImportError` on startup** — almost certainly the FastMCP version. This
project targets `4.0.0b2` exactly; v3 and the v4 alphas will not import.

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```

The test suite runs the server in-process via `fastmcp.Client`, so it also
verifies the pieces that only fail at call time: dependency injection of
timeouts, tag-based tool visibility, and path-injection rejection. Run it after
any FastMCP version bump — it doubles as the upgrade tripwire.

### Devcontainer

The devcontainer (see [Quickstart](#quickstart)) builds from a multi-stage
Dockerfile at `.devcontainer/Dockerfile`:

- `devcontainer` — used by VS Code for development
- `production` — used by Docker Compose for deployment

### Production with Docker Compose

```bash
docker compose up --build -d
```

Env vars come from a `.env` file alongside `docker-compose.yml`:

```bash
FITNESSE_BASE_URL=http://your-fitnesse-host:8080
FITNESSE_USERNAME=your-username
FITNESSE_PASSWORD=your-password
FITNESSE_READONLY=1
# FITNESSE_ALLOW_SHUTDOWN=1
# FITNESSE_UPLOAD_ROOT=/data/uploads
```

The server is then available at `http://localhost:8000/mcp` or through the stdio-to-html-proxy for stdio-only clients:

```json
{
  "mcpServers": {
    "fitnesse": {
      "command": "docker",
      "args": [
        "exec", "-i", "fitnesse-mcp",
        "fastmcp", "run", "http://127.0.0.1:8000/mcp"
      ]
    }
  }
}
```
