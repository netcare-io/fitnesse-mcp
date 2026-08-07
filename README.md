# fitnesse-mcp

Starter FastMCP workspace for local development in a VS Code devcontainer.

## Running the tests

```bash
pip install -e ".[dev]"
pytest tests/
```

## FitNesse REST tools included

The server now exposes a set of MCP tools mapped to FitNesse REST responders
from the official docs:

- `fitnesse_add_child_page`: `responder=addChild`
- `fitnesse_compare_history`: `responder=compareHistory`
- `fitnesse_create_dir`: `responder=createDir`
- `fitnesse_delete_page`: `responder=deletePage`
- `fitnesse_delete_file`: `responder=deleteFile`
- `fitnesse_edit_page`: `responder=edit` with redirect/nonExistent options
- `fitnesse_execute_search_properties`: `responder=executeSearchProperties`
- `fitnesse_list_files`: `responder=files`
- `fitnesse_get_page`: `responder=getPage`
- `fitnesse_import_pages`: `responder=import`
- `fitnesse_import_and_view`: `responder=importAndView`
- `fitnesse_get_instruction`: `responder=instruction`
- `fitnesse_move_page`: `responder=movePage`
- `fitnesse_list_names`: `responder=names`
- `fitnesse_get_new_page_form`: `responder=new`
- `fitnesse_get_packet`: `responder=packet`
- `fitnesse_get_page_data`: `responder=pageData`
- `fitnesse_get_page_history`: `responder=pageHistory`
- `fitnesse_get_properties`: `responder=properties`
- `fitnesse_publish`: `responder=publish`
- `fitnesse_purge_history`: `responder=purgeHistory`
- `fitnesse_get_raw`: `responder=raw`
- `fitnesse_get_refactor_screen`: `responder=refactor`
- `fitnesse_rename_file`: `responder=renameFile`
- `fitnesse_rename_page`: `responder=renamePage`
- `fitnesse_rollback_version`: `responder=rollback`
- `fitnesse_get_rss`: `responder=rss`
- `fitnesse_save_page_content`: `responder=saveData` (POST form data)
- `fitnesse_save_properties`: `responder=saveProperties` (POST form data)
- `fitnesse_search`: `responder=search`
- `fitnesse_get_search_form`: `responder=searchForm`
- `fitnesse_shutdown`: `responder=shutdown` _(requires `FITNESSE_ALLOW_SHUTDOWN=1`)_
- `fitnesse_stop_test`: `responder=stoptest`
- `fitnesse_run_suite`: `responder=suite` with suite filters/debug/nochunk
- `fitnesse_manage_symlink`: `responder=symlink`
- `fitnesse_run_test`: `responder=test`
- `fitnesse_get_test_history`: `responder=testHistory`
- `fitnesse_upload_file`: `responder=upload` (POST multipart form data)
- `fitnesse_get_versions`: `responder=versions`
- `fitnesse_view_version`: `responder=viewVersion`
- `fitnesse_where_used`: `responder=whereUsed`
- `fitnesse_get_variables`: `responder=variables`

Support utilities:

- `fitnesse_get_page_content`: convenience alias for `responder=edit`

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `FITNESSE_BASE_URL` | `http://localhost:8080` | FitNesse server base URL |
| `FITNESSE_USERNAME` | _(none)_ | Basic Auth username |
| `FITNESSE_PASSWORD` | _(none)_ | Basic Auth password |
| `FITNESSE_READONLY` | `false` | Set to `1`/`true`/`yes`/`on` to hide all write, execute, and control tools |
| `FITNESSE_ALLOW_SHUTDOWN` | `false` | Set to `1`/`true`/`yes`/`on` to expose `fitnesse_shutdown` |
| `FITNESSE_UPLOAD_ROOT` | _(none, uploads disabled)_ | Absolute path on the host; `fitnesse_upload_file` only reads files under this directory |
| `FITNESSE_MAX_RESPONSE_BYTES` | `1048576` (1 MB) | Truncate FitNesse responses larger than this many bytes |
| `FITNESSE_MAX_UPLOAD_BYTES` | `10485760` (10 MB) | Reject upload files larger than this many bytes |

Flag-style FitNesse inputs are supported by passing `None` values in query params (for example, `{"nohistory": None}`).

## Devcontainer

This project uses a multi-stage Dockerfile at `.devcontainer/Dockerfile`.

- `devcontainer` target: used by VS Code for development
- `production` target: used by Docker Compose for deployment

## Connecting an MCP client

There are two patterns depending on your use case.

### Pattern 1 — Stdio (simple, local)

No server process needed. The MCP client launches the server itself as a subprocess and communicates over stdin/stdout.

Configure your client (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "fitnesse": {
      "command": "fastmcp",
      "args": ["run", "/workspaces/fitnesse-mcp/my_server.py"],
      "env": {
        "FITNESSE_BASE_URL": "http://your-fitnesse-host:8080",
        "FITNESSE_USERNAME": "your-username",
        "FITNESSE_PASSWORD": "your-password"
      }
    }
  }
}
```

If the server runs in a Docker container, use `docker run` as the command. The `-i` flag keeps stdin open; `-e` forwards each variable from the `env` block into the container:

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
        "fitnesse-mcp:latest"
      ],
      "env": {
        "FITNESSE_BASE_URL": "http://your-fitnesse-host:8080",
        "FITNESSE_USERNAME": "your-username",
        "FITNESSE_PASSWORD": "your-password"
      }
    }
  }
}
```

### Pattern 2 — HTTP server (production, shared)

Use this when running in Docker or when multiple clients need to share one server instance.

First start the server:

```bash
FITNESSE_BASE_URL=http://your-fitnesse-host:8080 \
FITNESSE_USERNAME=your-username \
FITNESSE_PASSWORD=your-password \
fastmcp run my_server.py --transport http
```

Then connect a client directly via HTTP, or via the stdio proxy for clients that only speak stdio:

```json
{
  "mcpServers": {
    "fitnesse": {
      "command": "fastmcp",
      "args": ["run", "http://127.0.0.1:8000/mcp"]
    }
  }
}
```

## Interactive testing

To call tools interactively via a browser UI:

```bash
fastmcp-inspector
```

Then open the exact URL printed in the terminal (it includes
`?MCP_INSPECTOR_API_TOKEN=...`).

In devcontainers (especially as root), prefer the wrapper below to avoid noisy
keyring/DBus warnings:

```bash
./scripts/run-inspector.sh
```

If you want unfiltered raw logs for debugging:

```bash
INSPECTOR_RAW_LOGS=1 ./scripts/run-inspector.sh
```


## Production with Docker Compose

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

Pass env vars via a `.env` file alongside `docker-compose.prod.yml`:

```bash
FITNESSE_BASE_URL=http://your-fitnesse-host:8080
FITNESSE_USERNAME=your-username
FITNESSE_PASSWORD=your-password
# FITNESSE_READONLY=1
# FITNESSE_ALLOW_SHUTDOWN=1
```

Then open `http://localhost:8000/mcp`.
