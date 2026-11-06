# AGENTS.md

Guidance for coding agents working on this repository. Users of the server: see
[README.md](README.md).

## What this is

`fitnesse-mcp` is a FastMCP server that exposes FitNesse's REST responders as MCP tools. The
whole server is one module, [server.py](server.py); the tests are in [tests/](tests/).

- FastMCP is pinned exactly (`fastmcp==4.1.6` in [pyproject.toml](pyproject.toml)): it has
  broken things in minor releases. Bump it only on purpose and run the full test suite after.
- Python 3.12+; CI tests 3.12 and 3.13.
- The KDTA debug tools (`fitnesse_debug_*`, `fitnesse_table_*`) need netcare's FitNesse fork,
  which adds the `debugTest`/`debugTable` responders. A plain FitNesse does not have them.
  Features of newer forks get a fallback for older ones (look for "older fork" / "mod #…" in
  `server.py`).

## Commands

```bash
pip install -e ".[dev]"
pytest                              # the whole suite, ~40 s
pytest tests/test_debug.py -q       # debug tools only
python scripts/tools_list.py        # regenerate docs/tools.md after changing any tool
```

`tests/test_tools_doc.py` fails while [docs/tools.md](docs/tools.md) does not match the tools.

## How the code is organised

- **Configuration is read at import.** `FITNESSE_TOOLSETS` is required and decides which tools
  exist. Tests that need other settings set the environment and `importlib.reload(server)` (see
  `_reload` and the `debug_env` fixture in `tests/test_debug.py`; `tests/conftest.py` sets the
  default `all`).
- **Tool visibility goes by tags.** Each tool has one mode tag (`read`, `write`, `test`) and
  optionally an area (`debug`, `complete`). Extra env gates (`FITNESSE_FILES_ROOT`,
  `FITNESSE_ALLOW_SHUTDOWN`, `FITNESSE_ALLOW_ACCOUNT_ADMIN`) hide tools completely.
- **Debug session state** lives in `_DebugState`, per FitNesse login and URL (headers auth mode
  lets each HTTP request bring its own). It is in memory only.
- **HTTP to FitNesse** goes through urllib; tests replace it with fakes that record requests and
  return canned answers. No test needs a running FitNesse.

## Conventions

- **Tool descriptions are prompts for the model that calls the tools.** Write them in plain
  English, say what the result contains and what to call next, and keep them short: every
  client loads all of them. Text shared by several tools goes in a constant (e.g.
  `_TABLE_EDIT`). Regenerate `docs/tools.md` after any change.
- **Errors are `ToolError` with a next step** ("read it again and redo the change", "start one
  with fitnesse_debug_start"). Codes from the fork map to hints in `_DEBUG_CODE_HINTS`.
- **Every behaviour change gets a test**, including the fallback for older forks.
- **README:** user-facing changes go in the matching section; anything that breaks existing
  configurations also goes under "Breaking changes". Debug workflows:
  [docs/debug-workflows.md](docs/debug-workflows.md).
- Commit messages: imperative summary line, body with the why.

## Do not

- Commit or print secrets: `.env` and `fastmcp.json` hold credentials and are git-ignored; only
  the `*.example` files belong in the repo. `.devcontainer/devcontainer.json` often carries
  local changes (mounts, env files) that must not be committed.
- Push to `main`: it only accepts pull requests, and CI (`pytest` on both Pythons, `docker
  build`) must pass. Releases go through `scripts/release.sh` (see README, "CI and release").
- Against a live FitNesse: never pass `replace=true` to `fitnesse_debug_start` and never adopt
  someone else's session unless told to; check `fitnesse_debug_status` first and end every
  session you start. The `fitnesse_table_*` tools and `fitnesse_save_page_content` save real
  wiki pages: change only pages you created for the test, and restore them afterwards.
  Don't send real credentials to test servers you don't own.
