# fitnesse-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![FastMCP](https://img.shields.io/badge/FastMCP-4.1.6-blue.svg)](https://gofastmcp.com)

An MCP server that exposes [FitNesse](http://fitnesse.org/)'s REST responders as
tools, letting an MCP client read wiki pages, run tests and suites, manage the
files section, and inspect test history on a FitNesse instance.

Copyright (c) 2026 [netcare GmbH](https://github.com/netcare-io). Released under
the [MIT License](LICENSE).

---

## Requirements

- A reachable FitNesse instance
- Docker, or Python 3.12+ (FastMCP is pinned exactly, see
  [pyproject.toml](pyproject.toml): it has shipped breaking changes in minor
  releases)

---

## Quickstart

Register the published image with Claude Code, read-only for a first run:

```bash
claude mcp add fitnesse \
  -e FITNESSE_BASE_URL=http://your-fitnesse-host:8080 \
  -e FITNESSE_USERNAME=your-username -e FITNESSE_PASSWORD=your-password \
  -e FITNESSE_TOOLSETS=read \
  -- docker run --rm -i -e FITNESSE_BASE_URL -e FITNESSE_USERNAME \
     -e FITNESSE_PASSWORD -e FITNESSE_TOOLSETS ghcr.io/netcare-io/fitnesse-mcp:latest
```

`localhost` in `FITNESSE_BASE_URL` would be the container, not your host — see
[Troubleshooting](#troubleshooting). More options, including HTTP and a server
started from a checkout, are under [Running](#running).

---

## Security

This server gives an LLM client the ability to **delete pages, purge test
history, and roll back versions** on your FitNesse instance. Two controls limit
that, and both are opt-in:

- **Start with `FITNESSE_TOOLSETS=read`.** This hides every tool that
  changes pages or files or runs tests, leaving only the read tools (see
  [Tools](#tools) for counts). Add `write` and/or `test` deliberately once you
  know which operations you actually want the model to perform, e.g.
  `FITNESSE_TOOLSETS=read,test` to let it run tests but not edit pages.
- **`fitnesse_shutdown` is not registered at all** unless
  `FITNESSE_ALLOW_SHUTDOWN` is set. It stops the FitNesse server.
- **`fitnesse_save_account` is not registered at all** unless
  `FITNESSE_ALLOW_ACCOUNT_ADMIN` is set. It changes passwords and, as the
  FitNesse admin user, creates or deletes users.

- **The optional `X-FitNesse-Url` header is not checked** beyond being an
  `http(s)` URL: over the HTTP transport, whoever can reach the MCP endpoint
  can make the server call any host it can reach, including internal ones.
  In `FITNESSE_AUTH_MODE=env` the server also sends its own FitNesse login
  there. Expose an HTTP deployment only to people you trust with that, or use
  `FITNESSE_AUTH_MODE=headers` so the only credentials sent are the caller's.

Three further notes:

- `fitnesse_list_files`, `fitnesse_create_dir`, `fitnesse_upload_file`,
  `fitnesse_download_file`, `fitnesse_delete_file`, and `fitnesse_rename_file`
  are **not registered at all** unless `FITNESSE_FILES_ROOT` is set.
- Every files tool is confined to the resource root named by
  `FITNESSE_FILES_ROOT` on the **remote** FitNesse instance (default `files`,
  but some instances configure a different root): `files_path` must start
  with that root and may not contain `..`, and `filename` / `dirname` /
  `new_name` must be plain names without path separators. A call aiming
  outside it (e.g. `files_path: "FrontPage"`) is rejected before any request
  goes out, so these tools cannot reach wiki pages or other responders.
  Neither `fitnesse_upload_file` nor `fitnesse_download_file` touches local
  disk: content travels through the tool call itself, so both work even when
  the MCP server and the calling client don't share a filesystem.
  `fitnesse_download_file` returns the file's content directly in its
  result — as an inline image for `image/*` content, or as a text/binary
  resource otherwise. `fitnesse_upload_file` takes the content as an
  argument — plain text via `content`, or base64-encoded bytes via
  `content_base64` — instead of a local file path.
- Credentials in `claude_desktop_config.json` are stored in **cleartext**. For
  shared machines, prefer an HTTP server (see [Running](#running)), either with
  the credentials in its own environment or with `FITNESSE_AUTH_MODE=headers`,
  where every user sends their own FitNesse login and FitNesse's page rights
  apply per user.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `FITNESSE_BASE_URL` | `http://localhost:8080` | FitNesse server base URL. Over HTTP a request can name another instance in the optional `X-FitNesse-Url` header (see [Security](#security)) |
| `FITNESSE_AUTH_MODE` | `env` | `env`: every call uses `FITNESSE_USERNAME` / `FITNESSE_PASSWORD`. `headers`: every HTTP request brings its own FitNesse login in `X-FitNesse-Username` / `X-FitNesse-Password`; the env pair is ignored and a request without them fails. HTTP transport only |
| `FITNESSE_USERNAME` | _(none)_ | Basic Auth username (`env` mode) |
| `FITNESSE_PASSWORD` | _(none)_ | Basic Auth password (`env` mode) |
| `FITNESSE_TOOLSETS` | **required** | Comma-separated toolsets. `all` = `read,write,test` (every mode, no area); `all,debug,complete` grants everything. **Modes** `read`, `write`, `test` (named after FitNesse's `secure-read`/`-write`/`-test`) filter every tool: `read` reads pages, history, and files; `write` changes pages and files (also `fitnesse_shutdown` / `fitnesse_save_account`, which still need their own flag); `test` runs and stops tests. **Areas** `debug` (KDTA debug tools, `fitnesse_debug_*` / `fitnesse_table_*`; needs netcare's FitNesse fork with the `debugTest`/`debugTable` responders) and `complete` (16 lower-traffic tools, marked below) add tools, which are still filtered by the modes: `read,debug` shows only the reading debug tools. A missing or empty value, an area without a mode, or an unknown value stops the server at startup |
| `FITNESSE_ALLOW_SHUTDOWN` | `false` | `1`/`true`/`yes`/`on` exposes `fitnesse_shutdown` |
| `FITNESSE_ALLOW_ACCOUNT_ADMIN` | `false` | `1`/`true`/`yes`/`on` exposes `fitnesse_save_account` |
| `FITNESSE_FILES_ROOT` | _(none — files tools disabled)_ | Resource root of the files section on the FitNesse instance (e.g. `files`); enables `fitnesse_list_files`, `fitnesse_create_dir`, `fitnesse_upload_file`, `fitnesse_download_file`, `fitnesse_delete_file`, and `fitnesse_rename_file` |
| `FITNESSE_MAX_RESPONSE_BYTES` | `1048576` (1 MB) | Responses above this are truncated and flagged `"truncated": true` |
| `FITNESSE_FILES_MAX_TRANSFER_BYTES` | `10485760` (10 MB) | Uploads above this are rejected; downloads above this are truncated and rejected |
| `FITNESSE_RUN_TIMEOUT_SECONDS` | `120` | Read timeout for `fitnesse_run_test` and `fitnesse_run_suite`; `0` disables it. Applies per socket read, so it caps idle time, not total run time. The MCP client and any proxy have their own timeouts |
| `FITNESSE_DEBUG_TIMEOUT_SECONDS` | `660` | With `debug`: read timeout for debug documents; keep it above the server's `debugTest.timeoutMinutes` (default 10 min). `0` disables it |
| `FITNESSE_DEBUG_MAX_RESPONSE_BYTES` | `16777216` (16 MB) | Limit for raw debug answers (condensed before they reach the model); larger answers fail |
| `FITNESSE_DEBUG_MAX_CELL_CHARS` | `1500` | Table cells (e.g. stack traces) in debug results are cut to this length |

---

## Breaking changes in 1.0.0

Upgrading from 0.4.x. Prompts, skills, and allowlists that name tools or pass
the values below need updating. FitNesse 20261101 is the reference release
(see [FitNesse compatibility](#fitnesse-compatibility)).

### `FITNESSE_READONLY` and `FITNESSE_COMPLETE_TOOLSET` are replaced by `FITNESSE_TOOLSETS`

| 0.4.x | 1.0.0 |
|---|---|
| `FITNESSE_READONLY=1` | `FITNESSE_TOOLSETS=read` (`read,debug` with the KDTA debug tools) |
| `FITNESSE_READONLY` unset or `0` | `FITNESSE_TOOLSETS=all` (= `read,write,test`) |
| `FITNESSE_COMPLETE_TOOLSET=1` | add `complete`, e.g. `FITNESSE_TOOLSETS=read,complete` or `all,complete` |
| — | finer: e.g. `read,test` (run tests, no edits) or `read,write` (edit, no runs) |

**`FITNESSE_TOOLSETS` is required**: without it the server stops at startup,
so no configuration silently gets more permissions than before. Both old
variables are no longer read; remove them.
`fitnesse_stop_test` needs `test`; `fitnesse_shutdown` and
`fitnesse_save_account` need `write` (and their own flag). The KDTA debug
tools (new) need `debug` plus the mode of what they do (see
[KDTA debug mode](#kdta-debug-mode-needs-debug-in-fitnesse_toolsets)). Areas
(`debug`, `complete`) are never included in `all`; `all,debug,complete`
grants everything.

### Removed and renamed tools

| 0.4.x | 1.0.0 |
|---|---|
| `fitnesse_execute_search_properties` | `fitnesse_search_properties` (needs `complete`) |

Parameter mapping for `fitnesse_search_properties`:

| 0.4.x | 1.0.0 |
|---|---|
| `page_type` (one value) | `page_types` (list of `Static`, `Test`, `Suite`) |
| `action` (string) | `actions` (list; `Any` ignores the group) |
| `exclude_setup`, `exclude_teardown` | `special` (list of `Any`, `SetUp`, `TearDown`: listed kinds are included, unlisted excluded) |
| `exclude_obsolete` | removed; FitNesse never evaluated it |
| — | new: `security`, `prune`, `search_scope` |
| `page_path` required | `page_path` optional (default: root) |

### Changed values

- **Page types** are `Static`, `Test`, `Suite`. `Normal` and `Default` are
  rejected; use `Static`. Affects `fitnesse_add_child_page`,
  `fitnesse_get_new_page_form`, and `fitnesse_save_properties`.
- **`fitnesse_save_properties` `attributes`** use FitNesse's property names:

  | 0.4.x | 1.0.0 |
  |---|---|
  | `Secure-Read`, `Secure-Edit`, `Secure-Test` | `secure-read`, `secure-write`, `secure-test` |
  | `Test`, `Suite` | removed; set `page_type` |
  | — | new: `DisableTestHistory` |

- **`fitnesse_search` `search_type`** accepts only `title` or `content`.
- **`fitnesse_save_page_content` `edit_time` / `ticket_id`** must be numeric.

### Changed behaviour

- **`fitnesse_save_properties` merges instead of resetting.** Omitted
  `page_type`, `suites`, `help_text`, and `attributes` keep their current
  values. 0.4.x always sent `page_type` (default `Normal`, as `pageType`,
  which FitNesse 20251025 ignores), and FitNesse dropped the omitted tags,
  help text, and attributes. To clear a value, pass it empty.
- **`fitnesse_save_page_content` keeps help text and tags.** FitNesse removes
  them when they are not sent; the tool now sends the current ones unless
  `help_text` / `suites` are given.
- **Both save tools fail when they need the current properties and cannot
  read them** (`ToolError`, nothing saved), i.e. when a value above is
  omitted. 0.4.x saved without them.
- **`fitnesse_save_page_content` overwrites without `edit_time`.** In 0.4.x a
  page saved once through the UI could silently turn the next save into
  FitNesse's merge form (status 200, nothing saved). With `edit_time` and
  `ticket_id` from `fitnesse_get_page_data`, a conflicting save now fails
  with "Not saved" instead of returning the merge form.
- **`fitnesse_get_page_data`** also returns `edit_time` and `ticket_id` and
  makes two requests (`edit`, then `pageData`).

### Changed tool set

Visible without `complete` now (0.4.x: only with `FITNESSE_COMPLETE_TOOLSET` or not at all):

| Tool | Kind | Needs in `FITNESSE_TOOLSETS` |
|---|---|---|
| `fitnesse_search` | read | `read` |
| `fitnesse_compare_versions` | read | `read` |
| `fitnesse_get_suite_overview` | read | `read` |
| `fitnesse_replace` | **write, destructive** (saves every matching page below `page_path`) | `write` |

Review allowlists and client permissions for `fitnesse_replace`. New gated
tools (`FITNESSE_ALLOW_ACCOUNT_ADMIN`, the `debug` toolset) are off unless
enabled.

### Dependencies

- FastMCP is pinned to `4.1.6` (0.4.x: `4.1.1`). Python 3.12 or newer, as
  before.

---

## Tools

[docs/tools.md](docs/tools.md) lists every tool with its toolset, gates and full
description; regenerate it with `python scripts/tools_list.py` after changing a tool
(a test fails while it is out of date).

27 tools with `FITNESSE_TOOLSETS=all` and no other flags, each mapping to
one FitNesse responder. Set `FITNESSE_FILES_ROOT` to the files-section
resource root on your FitNesse instance to also expose `fitnesse_list_files`,
`fitnesse_create_dir`, `fitnesse_upload_file`, `fitnesse_download_file`,
`fitnesse_delete_file`, and `fitnesse_rename_file` (33 total), add `complete`
to `FITNESSE_TOOLSETS` for 16 additional lower-traffic tools (49), `debug` for
the 15 KDTA debug tools (64), `FITNESSE_ALLOW_SHUTDOWN=1` for
`fitnesse_shutdown`, and `FITNESSE_ALLOW_ACCOUNT_ADMIN=1` for
`fitnesse_save_account` (66). The modes in `FITNESSE_TOOLSETS` filter all of
these. Visible tools:

| `FITNESSE_TOOLSETS` | no flags | + `FITNESSE_FILES_ROOT` | + `FITNESSE_FILES_ROOT`, `,complete` | + `,complete`, all flags |
|---|---|---|---|---|
| `read` | 12 | 14 | 29 | 29 |
| `read,debug` | 17 | 19 | 34 | 34 |
| `read,test` | 15 | 17 | 32 | 32 |
| `read,test,debug` | 24 | 26 | 41 | 41 |
| `read,write` | 24 | 30 | 46 | 48 |
| `all` (= `read,write,test`) | 27 | 33 | 49 | 51 |
| `all,debug` | 42 | 48 | 64 | 66 |

`write` or `test` without `read` work too, but leave the model blind to what
it changes or runs.

### FitNesse compatibility

Checked against the [RestfulServices](https://fitnesse.org/FitNesse/UserGuide/AdministeringFitNesse/RestfulServices.html)
documentation and the source of the two latest releases:

| FitNesse release | Status |
|---|---|
| **20261101** (latest, Nov 2026) | Fully supported. Every responder and input of the RestfulServices page is covered, except the browser-only forms listed below. |
| **20251025** | Supported. Everything works except two inputs that 20251025 ignores: `fitnesse_delete_page(delete_versions)` and the navigation actions (`RecentChanges`, `Files`, `Search`) in `fitnesse_search_properties(actions)`. |
| older | Not tested. |

Compatibility details:

- **Page type input.** 20251025 reads only `PageType`; 20261101 renamed it to
  `pageType` and still accepts `PageType`. The server sends `PageType` for
  `saveProperties` and `searchProperties`, so both releases set and match the
  page type. (`addChild` and `new` always used `pageType`.)
- **`saveData` / `saveProperties` remove what is not sent** (help text, tags,
  attributes) in both releases. `fitnesse_save_page_content` and
  `fitnesse_save_properties` therefore read the current properties first and
  send them back unless you pass new values.
- **`saveData` can answer with a merge form instead of saving** (both
  releases; there is no separate `merge` responder). FitNesse compares the
  sent `editTime` with the page's last save since server start; a missing
  `editTime` counts as 0, so a page saved once through the UI would silently
  reject every later save. `fitnesse_save_page_content` therefore overwrites
  when `edit_time` is omitted (it sends the largest possible `editTime`). With
  `edit_time` and `ticket_id` (both returned by `fitnesse_get_page_data`, which
  reads them from the `edit` form with read permission) the conflict check
  stays on, and a merge form makes the tool fail with "Not saved" instead of
  reporting success.
- **`executeSearchProperties` is deprecated** and is the same responder as
  `searchProperties` in both releases; it never evaluated the
  `ExcludeSetUp`/`ExcludeTearDown`/`ExcludeObsolete` inputs. Use
  `fitnesse_search_properties` (`special`, `prune`, `security`).
- **`testHistory` `results`/`trim`** only shape the HTML view;
  `fitnesse_get_test_history` accepts them with `format='html'` only.
- **`stoptest` `id`**: FitNesse reports it in the `X-FitNesse-Test-Id` header of
  a running test, but `fitnesse_run_test`/`fitnesse_run_suite` return only when
  the run has ended, so stop running tests with `fitnesse_stop_test()` (all).
- **Not exposed** (browser forms without data of their own): `account`,
  `deleteConfirmation`, `renameConfirmation`, the `edit` inputs
  `redirectToReferer`/`redirectAction`, and the UI-internal `lazyPageTree`
  (20261101).

<details>
<summary><b>Full tool list</b></summary>

### Pages — read

| Tool | Responder |
|---|---|
| `fitnesse_get_page` | `getPage` _(needs `complete`)_ |
| `fitnesse_get_raw` | `raw` _(needs `complete`)_ |
| `fitnesse_get_page_data` | `pageData`; also returns `edit_time` / `ticket_id` (from `edit`) for the conflict check of `fitnesse_save_page_content` |
| `fitnesse_get_packet` | `packet` — all tables on a page, as JSON _(needs `complete`)_ |
| `fitnesse_get_properties` | `properties` |
| `fitnesse_get_variables` | `variables` |
| `fitnesse_list_names` | `names` |
| `fitnesse_edit_page` | `edit` — with redirect/nonExistent options _(needs `complete`)_ |
| `fitnesse_get_page_content` | `edit` — convenience alias _(needs `complete`)_ |
| `fitnesse_get_new_page_form` | `new` _(needs `complete`)_ |
| `fitnesse_get_refactor_screen` | `refactor` _(needs `complete`)_ |
| `fitnesse_get_rss` | `rss` |
| `fitnesse_get_proxy_data` | `proxy` — serialized page data used by import _(needs `complete`)_ |

### Pages — write

| Tool | Responder |
|---|---|
| `fitnesse_add_child_page` | `addChild` — with help text and tags |
| `fitnesse_save_page_content` | `saveData` (POST form data); keeps the page's help text and tags unless `help_text` / `suites` are given; overwrites without `edit_time`, fails on a conflict with it |
| `fitnesse_save_properties` | `saveProperties` (POST form data); arguments left out keep their current value, `attributes` is the full set of flags |
| `fitnesse_rename_page` | `renamePage` — optional `refactorReferences` |
| `fitnesse_move_page` | `movePage` — optional `refactorReferences` |
| `fitnesse_delete_page` | `deletePage` — optional `deleteVersions` (20261101) |
| `fitnesse_replace` | `replace` — text (regex) or method replace below a page |
| `fitnesse_manage_symlink` | `symlink` |
| `fitnesse_import_pages` | `import` |
| `fitnesse_import_and_view` | `importAndView` |
| `fitnesse_publish` | `publish` _(needs `complete`)_ |

### Tests

| Tool | Responder | Mode |
|---|---|---|
| `fitnesse_run_test` | `test` — `format` xml/junit/text, nohistory | execute |
| `fitnesse_run_suite` | `suite` — tag filters, firstTest, partitions, `format` xml/junit/text, debug, nochunk | execute |
| `fitnesse_get_suite_overview` | `overview` — suite tree with latest results | read |
| `fitnesse_partition_preview` | `partition` — TSV or HTML _(needs `complete`)_ | read |
| `fitnesse_get_instruction` | `instruction` — Slim instructions _(needs `complete`)_ | read |
| `fitnesse_stop_test` | `stoptest` | control |
| `fitnesse_shutdown` | `shutdown` _(needs `FITNESSE_ALLOW_SHUTDOWN`)_ | control |
| `fitnesse_save_account` | `saveAccount` — change password, create/delete user _(needs `FITNESSE_ALLOW_ACCOUNT_ADMIN`)_ | control |

### History & versions

| Tool | Responder | Mode |
|---|---|---|
| `fitnesse_get_test_history` | `testHistory` — XML, or HTML with `results`/`trim` _(needs `complete`)_ | read |
| `fitnesse_get_execution_log` | `executionLog` _(needs `complete`)_ | read |
| `fitnesse_get_page_history` | `pageHistory` _(drops `executionLog` `command`/`stdOut`/`stdErr` unless `include_command`/`include_stdout`/`include_stderr`)_ | read |
| `fitnesse_compare_history` | `compareHistory` | read |
| `fitnesse_get_versions` | `versions` | read |
| `fitnesse_view_version` | `viewVersion` | read |
| `fitnesse_compare_versions` | `compareVersions` — one version against latest, or two | read |
| `fitnesse_rollback_version` | `rollback` | write |
| `fitnesse_purge_history` | `purgeHistory` — page subtree, or all with `purge_global` | write |

### Search

| Tool | Responder |
|---|---|
| `fitnesse_search` | `search` — title/content, method search, `searchScope` — read |
| `fitnesse_search_properties` | `searchProperties` — page types, tags, actions, security, SetUp/TearDown, skipped, `searchScope` — read _(needs `complete`)_ |
| `fitnesse_get_search_form` | `searchForm` — read _(needs `complete`)_ |
| `fitnesse_where_used` | `whereUsed` — read _(needs `complete`)_ |

### Files section

| Tool | Responder | Mode |
|---|---|---|
| `fitnesse_list_files` | `files` _(needs `FITNESSE_FILES_ROOT`)_ | read |
| `fitnesse_download_file` | _(direct file GET, returned inline as image/text/binary)_ _(needs `FITNESSE_FILES_ROOT`)_ | read |
| `fitnesse_create_dir` | `createDir` _(needs `FITNESSE_FILES_ROOT`)_ | write |
| `fitnesse_upload_file` | `upload` (POST multipart, content passed inline) _(needs `FITNESSE_FILES_ROOT`)_ | write |
| `fitnesse_rename_file` | `renameFile` _(needs `FITNESSE_FILES_ROOT`)_ | write |
| `fitnesse_delete_file` | `deleteFile` _(needs `FITNESSE_FILES_ROOT`)_ | write |

### KDTA debug mode _(needs `debug` in `FITNESSE_TOOLSETS`)_

Run a test table by table in a long-lived test process (`responder=debugTest`)
and edit single tables of a page (`responder=debugTable`). Needs netcare's
FitNesse fork; on other FitNesse servers leave `debug` out. Results are not
written to the test history.

All need `debug` in `FITNESSE_TOOLSETS`, plus the listed mode.

| Tool | Action | Mode |
|---|---|---|
| `fitnesse_debug_start` | `status`, then `start`; refuses if a session of the account runs on the same page (forks before mod #159: anywhere), unless `replace`, and while this server's own session still runs. With `suite`: also `suitePages` | test |
| `fitnesse_debug_status` | `status` plus new `log` lines; `sessions` lists all of the account's (mod #159); `adopt` continues a running session, with several the one named by `session` | read |
| `fitnesse_debug_run_wiki` | `runWiki` — wiki tables in the context of the current page, no SetUp/TearDown; `restart` runs `nextPage` first | test |
| `fitnesse_debug_page_tables` | `pageHtml`, split into tables; each with `source_page`/`source_index` for editing (tables of included pages live on that page) | read |
| `fitnesse_debug_run_page` | `nextPage` (on page change or `restart`), `pageHtml`, `runTable`. `setup_only` runs just the SetUp tables; a later call without `tables` skips them (`skipped_setup`). `only_failures` returns only tables with `[fail]`/`[error]` | test |
| `fitnesse_debug_inspect` | `keywordsJson` (without filter only the categories with their number of keywords; with filter the matching keywords in full), `definedActions` (the defined actions loaded by `define actions at`: `debugTest definedActions` as JSON, on older forks parsed from `keywords`; filterable), `dynamicVariables`, `scope`, `liveView` (VNC password removed) | read |
| `fitnesse_debug_defined_action_source` | `debugTable definedAction` (with the session, or `page=` without one). Forks without it: page of the defined action from `keywords`, then `debugTable source` of that page, cut to the definition block as FitLibrary reads it (header first or after `----`, up to the next `----`; refuses a header found twice) | read |
| `fitnesse_debug_end` | `end` or `abort`, with the unread log | test |
| `fitnesse_table_source` | `debugTable source` | read |
| `fitnesse_table_save`, `fitnesse_table_insert`, `fitnesse_table_delete`, `fitnesse_table_move`, `fitnesse_table_save_text`, `fitnesse_table_move_text` | `debugTable save`/`saveBlock`, `insert`, `delete`, `move`, `saveText`, `moveText` — saves the wiki page, refused with 409 if it changed since it was read. The result's `source` is the page after the change (from the answer since mod #157, else read again). With `reload_defined_actions=true` every `fitnesse_table_*` edit sends `reload=true` with the session, so the fork reloads the page's defined actions; forks that don't answer `reloaded` get `\|define actions at\|.<page>\|` via `runWiki` instead | write |

Dependencies between the tools (the same rules go to MCP clients as server
`instructions` when a session can be used, i.e. with `read,test,debug` in
`FITNESSE_TOOLSETS`; the part about editing tables only
with `write` as well):

```
fitnesse_debug_start ──or── fitnesse_debug_status(adopt=true)      opens / takes over the session
        │
        ├─ fitnesse_debug_run_page(setup_only=true)                   SetUp first: browser, defined actions
        ├─ fitnesse_debug_inspect(kind='keywords')                    categories; with filter: exact wording
        ├─ fitnesse_debug_inspect(kind='definedActions')              defined actions loaded in the session
        │     └─ fitnesse_debug_defined_action_source ──> fitnesse_table_save(…, reload_defined_actions=true)
        ├─ fitnesse_debug_run_wiki                                    ad-hoc tables, no SetUp/TearDown
        ├─ fitnesse_debug_page_tables ──> fitnesse_debug_run_page(tables=[…])
        ├─ fitnesse_debug_status                                      progress, log, busy
        │
fitnesse_debug_end                                                    tear down, frees the session

fitnesse_table_source ──> fitnesse_table_save / _insert / _delete / _move / _save_text / _move_text   no session needed
                              └─ result.source ──> next edit
```

- **Session required:** `run_wiki`, `run_page`, `page_tables`, `inspect`,
  `defined_action_source` (unless given `page_path`) and `end` fail until `fitnesse_debug_start` or `fitnesse_debug_status(adopt=true)`
  has run on this MCP server. `fitnesse_debug_status` (without `adopt`) and the
  `fitnesse_table_*` tools work without a session.
- **State carries over** between `run_wiki` and `run_page` calls of the same
  page (open browser, scope, variables). `run_page` with a different page, or
  `restart=true` on either tool, first ends the current page's test (its tear
  down comes back as `previous_page_teardown`).
- **SetUp first.** `run_wiki` does not run SetUp/TearDown. Right after
  `fitnesse_debug_start`, run `fitnesse_debug_run_page(setup_only=true)`: the
  SetUp opens the browser and loads the defined actions, so `inspect` and
  `run_wiki` have them. SetUp tables are those whose outermost include is a
  page named `SetUp` or `SuiteSetUp`. A later `run_page` without `tables`
  leaves them out (`skipped_setup`) instead of running them twice;
  `restart=true` or another page runs them again.
- **Suite sessions:** `fitnesse_debug_start(suite=true)` returns `pages`;
  call `fitnesse_debug_run_page` for each page with `runnable=true` in that
  order.
- **Results:** `run_wiki`, `run_page` and `end` return every table as text,
  one row per line, cells prefixed with `[pass]`, `[fail]`, `[error]` or
  `[ignore]`. Below each table follow what belongs to it: folding areas after
  it (`after: <title>`, e.g. FitLibrary's *show after* or logs), links
  (`link in row R, column C: text -> url`) and images. Inline screenshots
  (`[image N]` in a cell) come back as MCP image content, at most 4 per call;
  `images` says which one is where or why one was left out.
  `run_page(only_failures=true)` returns only the tables with a `[fail]` or
  `[error]` cell, under their usual numbers (`tables_without_failures` counts
  the others).
- **Keywords:** `inspect(kind='keywords')` without filter lists only the
  categories with their number of keywords (the full list runs to tens of
  kilobytes); pass a word or a category such as `Web / Browser` as `filter`
  for syntax and parameters.
- **Table edits:** `save`/`insert`/`delete`/`move`/`save_text`/`move_text`
  take as `original` the unchanged wiki of exactly the table or text they
  change (`tables[index]` / `texts[index]` of `fitnesse_table_source`). Each
  result carries `source`, the page after the change: take the next edit's
  `original` and indices from it, as indices shift. On 409 without a code
  (page changed) read `fitnesse_table_source` again and redo the change. A
  running session picks up the change the next time the tables are run.
- **Defined actions:** `inspect(kind='definedActions')` lists what
  `|define actions at|` has loaded into the session (empty before). To edit
  one, `fitnesse_debug_defined_action_source` gives `page`, `index`, `last`
  and `original` for `fitnesse_table_save(..., reload_defined_actions=true)`,
  which makes the session run the new version at once. Forks with
  `debugTable definedAction` (mod #156 + lib #84) pick the definition as a
  run would; on older ones (400, or 501 `unsupported`) the MCP finds the block
  itself and refuses a header that is on the page twice. Check
  `reload.reloaded` in the save result: `false` (with `error`/`code` in a 200
  answer) means the page is saved but the session still runs the old
  version. 409 `changed` means the definition moved on its page since the
  session loaded it: run `|define actions at|.<page>|` with `run_wiki` (or
  repeat the edit with `reload_defined_actions=true`), then call
  `defined_action_source` again.
- **Two different table indices:** `fitnesse_debug_page_tables` counts every
  table a run executes (SetUp, TearDown, included pages);
  `fitnesse_table_source` counts only the page's own tables. Never pass an
  index from one to the other: `page_tables` gives each table its
  `source_page` and `source_index` for editing. Tables of included pages are
  edited on that page, which changes every page that includes it.
- **`busy=true`** from `fitnesse_debug_status`: wait until it is false, or
  call `fitnesse_debug_end(abort=true)`; a `run_wiki`/`run_page` call fails
  at once meanwhile instead of queueing.

Notes:

- **One debug session per page and FitNesse account** (mod #159; up to
  `debugTest.maxSessionsPerUser`, default 5, and `debugTest.maxSessions`,
  default 20, on the server; 429 `tooManySessions` beyond). Starting one on
  a page kills the account's running session on that page without tear
  down, so `fitnesse_debug_start` refuses unless told otherwise; sessions on
  other pages, e.g. a person's in the browser, keep running. The MCP server
  itself works with one session at a time. Every later action sends the
  session id; if someone replaced the session, the server answers 409
  `replaced` and the MCP server forgets it. Older forks allow **one session
  per account**: there a person in the browser and the MCP server with the
  same account replace each other, so give the MCP server its own FitNesse
  account.
- Use Basic Auth (`FITNESSE_USERNAME`/`FITNESSE_PASSWORD`): it bypasses the
  SSO proxy. A non-JSON answer is reported as a login problem. Without a
  login (a FitNesse without authenticator) the session belongs to the
  `debugTestSession` cookie that `start` sets; the MCP server sends it with
  every session call. It keeps the cookie only in memory: after a restart of
  the MCP server its old session cannot be adopted and ends after the idle
  timeout (30 min). With an account adopt works after a restart.
- `debug` without `read` or `test` in `FITNESSE_TOOLSETS` leaves the debug
  mode half usable (no session, or no inspecting); the server logs a warning
  at startup naming the hidden tools.
- Documents of a session run one after the other; a second one fails at once
  instead of queueing. `fitnesse_debug_status` (with the log) and
  `fitnesse_debug_end(abort=true)` work while a document runs.
- End the session when done: it holds one of `debugTest.maxSessions` and
  possibly grid browsers until `debugTest.idleMinutes` ends it.

</details>

Flag-style FitNesse inputs are supported by passing `None` as a query param
value — for example `{"nohistory": None}` produces `?nohistory`.

---

## Running

```bash
# stdio (local MCP clients), from a checkout after `pip install .`
FITNESSE_BASE_URL=http://your-fitnesse-host:8080 FITNESSE_TOOLSETS=read fitnesse-mcp

# HTTP, as in docker-compose.yml
FASTMCP_TRANSPORT=http FASTMCP_HOST=0.0.0.0 FASTMCP_PORT=8000 fitnesse-mcp
# or: fastmcp run server.py --transport http --host 0.0.0.0 --port 8000
```

Register it with Claude Code, here locally over stdio, allowed to read and run
tests but not to edit pages:

```bash
claude mcp add fitnesse \
  -e FITNESSE_BASE_URL=http://your-fitnesse-host:8080 \
  -e FITNESSE_USERNAME=your-username -e FITNESSE_PASSWORD=your-password \
  -e FITNESSE_TOOLSETS=read,test \
  -- fitnesse-mcp
```

Or connect to a shared HTTP deployment running with
`FITNESSE_AUTH_MODE=headers`, using your own FitNesse login:

```bash
claude mcp add --transport http fitnesse https://fitnesse-mcp.example/mcp \
  --header "X-FitNesse-Username: ..." --header "X-FitNesse-Password: ..."
```

Add `--header "X-FitNesse-Url: https://other-fitnesse.example"` to work on
another FitNesse instance than the server's `FITNESSE_BASE_URL`; debug
sessions are kept apart per instance and user.

With `headers`, FitNesse sees each user's own account: its page rights apply,
and changes and debug sessions are recorded under that user. The headers carry
the password, so serve the endpoint over HTTPS only (e.g. behind a
TLS-terminating reverse proxy). In the default `env` mode a shared server calls
FitNesse with the one account from its own environment, so every client acts
as that user, and the MCP endpoint itself has no authentication: run it only
where everyone who can reach the port may use that account.

Other clients take the same settings as JSON, e.g. for stdio:

```json
{
  "mcpServers": {
    "fitnesse": {
      "command": "fitnesse-mcp",
      "env": {
        "FITNESSE_BASE_URL": "http://your-fitnesse-host:8080",
        "FITNESSE_USERNAME": "your-username",
        "FITNESSE_PASSWORD": "your-password",
        "FITNESSE_TOOLSETS": "read"
      }
    }
  }
}
```

or `{"url": "http://localhost:8000/mcp"}` for HTTP. Clients that only speak
stdio reach an HTTP server through FastMCP's proxy: `fastmcp run
http://localhost:8000/mcp`.

### Docker

Every release is published to `ghcr.io/netcare-io/fitnesse-mcp` as `<version>`,
`<major>.<minor>` and `latest` (from 1.0.0 on):

```bash
# HTTP, every user brings their own FitNesse login
docker run --rm -p 8000:8000 -e FITNESSE_BASE_URL=http://your-fitnesse-host:8080 \
  -e FITNESSE_TOOLSETS=all -e FITNESSE_AUTH_MODE=headers \
  -e FASTMCP_TRANSPORT=http -e FASTMCP_HOST=0.0.0.0 ghcr.io/netcare-io/fitnesse-mcp:latest

# stdio, e.g. as the command of a local MCP client
docker run --rm -i -e FITNESSE_BASE_URL -e FITNESSE_USERNAME -e FITNESSE_PASSWORD \
  -e FITNESSE_TOOLSETS ghcr.io/netcare-io/fitnesse-mcp:latest
```

With stdio, **every variable needs its own `-e` flag**: `-e NAME` without a
value forwards it from the client's environment (the `env` block of its
config), and anything not forwarded is silently missing inside the container.

`docker compose up --build -d` builds the `production` image locally and reads
the variables from `.env` (see [.env.example](.env.example)); it serves HTTP on
port 8000 inside the container (uncomment `ports` in
[docker-compose.yml](docker-compose.yml) to publish it).

---

## Interactive testing

Create a `fastmcp.json` (see [fastmcp.example.json](fastmcp.example.json)),
then start the MCP Inspector from the project directory in the devcontainer:

```bash
fastmcp-inspector
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

**A write, run, or debug tool is missing** — `FITNESSE_TOOLSETS` lacks
`write`, `test`, or `debug` (or `complete` for the marked tools). Debug tools
also need the mode of what they do, e.g. `fitnesse_debug_start` needs `test`.

**`FITNESSE_TOOLSETS is required` at startup** — set it, e.g. `read`,
`all`, or `all,debug,complete`. It replaces `FITNESSE_READONLY` and `FITNESSE_COMPLETE_TOOLSET` (see
[Breaking changes](#breaking-changes-in-100)).

**`FITNESSE_TOOLSETS=...: ... need at least one of read, write, test` at
startup** — `debug` and `complete` only add tools within the modes; add a
mode, e.g. `read,debug`.

**A defined action runs its old version after editing** — FitLibrary used to
prefer a `<page>.wiki` file next to the page's `content.txt` when loading
defined actions, while FitNesse (and `fitnesse_table_source`/`_save`) use
`content.txt`. The session then runs the old file although the page shows the
new text. Fixed in netcare's FitLibrary (lib PR #83); until that is deployed,
remove the stale `.wiki` file.

**`fitnesse_debug_inspect(kind='definedActions')` is empty** — the session
lists only what `|define actions at|` has loaded; run those tables (usually
in SetUp) with `fitnesse_debug_run_page(tables=[...])` first.

**`"truncated": true` in a response** — the body exceeded
`FITNESSE_MAX_RESPONSE_BYTES` and was cut. Common on suite runs with
`includehtml`. Raise the limit or narrow the request.

**`Invalid path`** — the page path contained `?`, `#`, `..`, or a null byte.
FitNesse page paths are dotted (`FrontPage.MySuite.MyTest`) with no leading
slash.

**`ImportError` on startup** — almost certainly the FastMCP version. This
project targets `4.1.6` exactly; v3 and the v4 prereleases are not supported.

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

Open the project in VS Code with the
[Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)
and accept **Reopen in Container** (or run **Dev Containers: Reopen in
Container** from `F1`). Python and all dependencies are installed inside the
container. It builds from a multi-stage Dockerfile at
`.devcontainer/Dockerfile`:

- `devcontainer` — used by VS Code for development
- `test` — installs dev dependencies and runs `pytest`
- `production` — the published image, also used by Docker Compose

An MCP client on the host can use the running devcontainer (named
`fitnesse-mcp-devcontainer`) directly:

```json
{
  "mcpServers": {
    "fitnesse": {
      "command": "docker",
      "args": ["exec", "-i", "fitnesse-mcp-devcontainer", "fitnesse-mcp"]
    }
  }
}
```

`docker exec` passes no `FITNESSE_*` variables; the server sees the
environment the container was created with (`containerEnv` and `--env-file`
in `.devcontainer/devcontainer.json`). Rebuild the devcontainer after changing
them.

### CI and release

`main` only accepts pull requests. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml))
runs `pytest` on Python 3.12 and 3.13 and builds the production image for
every PR and every push to `main`.

To release, run `scripts/release.sh <version>` twice:

1. On `main`: bumps `version` in `pyproject.toml` on a new branch
   `release/v<version>` and opens a PR (prints the link if `gh` is missing).
2. Merge the PR once CI is green.
3. `git pull`, then the same command again on `main`: tags `v<version>` and
   pushes the tag.

The tag starts [.github/workflows/release.yml](.github/workflows/release.yml):
it checks that the tag matches `pyproject.toml` and is on `main`, runs the
tests, pushes the image to `ghcr.io/netcare-io/fitnesse-mcp` (`<version>`,
`<major>.<minor>`, `latest`), and creates the GitHub release with generated
notes.
