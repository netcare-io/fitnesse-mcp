# Debugging with the KDTA debug tools

Typical flows for an agent (or a person driving one) that debugs FitNesse tests with this MCP
server. The tools need netcare's FitNesse fork with the `debugTest`/`debugTable` responders and
`FITNESSE_TOOLSETS` with `debug`: `read,test,debug` to run and inspect, plus `write` to change
pages. Every tool and its full description: [tools.md](tools.md).

## The tools at a glance

| Group | Tools | Mode |
|---|---|---|
| Session | `fitnesse_debug_start`, `fitnesse_debug_end`, `fitnesse_debug_status` | test (status: read) |
| Run | `fitnesse_debug_run_page` (whole page, chosen tables, `setup_only`, `only_failures`), `fitnesse_debug_run_wiki` (any tables) | test |
| Look | `fitnesse_debug_inspect` (`keywords`, `definedActions`, `dynamicVariables`, `scope`, `liveView`), `fitnesse_debug_page_tables` | read |
| Source | `fitnesse_table_source`, `fitnesse_debug_defined_action_source` | read |
| Edit | `fitnesse_table_save`, `_insert`, `_delete`, `_move`, `_save_text`, `_move_text` | write |
| Find the failure | `fitnesse_run_test`, `fitnesse_get_page_history` (not debug tools) | test / read |

What to keep in mind:

- **A session is a living test process.** Browsers, objects in scope and variables carry over
  from one call to the next until `fitnesse_debug_end`. Nothing goes into the test history.
- **SetUp first.** `fitnesse_debug_run_wiki` does not add the page's SetUp. Run
  `fitnesse_debug_run_page(setup_only=true)` right after the start, otherwise defined actions,
  the browser and fixtures are missing.
- **Trying out changes nothing.** `run_wiki` only runs tables; the `fitnesse_table_*` tools
  save the wiki page for real.
- **Two kinds of table index.** `fitnesse_debug_page_tables` counts everything a run executes
  (SetUp, TearDown, included pages); `fitnesse_table_source` counts only a page's own tables.
  `page_tables` gives each table's `source_page` and `source_index` for editing.
- **Results.** Cells are prefixed `[pass]`, `[fail]`, `[error]`, `[ignore]`; `counts` are the
  call's, `page_totals` the page's so far. Inline screenshots come back as images.
- **One session per page and account** (mod #159; older forks: per account). This server works
  with one session at a time. Always end it: it holds a server slot and browsers.

## 1. Step through a failing test

The most common case: a test fails and the cause is unclear.

```
fitnesse_run_test / fitnesse_get_page_history   which page and table fail
fitnesse_debug_start(page)
fitnesse_debug_run_page(setup_only=true)        browser, defined actions, fixtures
fitnesse_debug_page_tables                      indices; the failing table is k
fitnesse_debug_run_page(tables=[0..k-1])        up to just before the failure
fitnesse_debug_inspect(kind='liveView')         optional: link for a person to watch
fitnesse_debug_run_page(tables=[k])             reproduce: read [fail]/[error], screenshot
fitnesse_debug_run_wiki('|…variant…|')          try hypotheses: other locator, a wait
fitnesse_debug_inspect(kind='dynamicVariables') check values
fitnesse_debug_end
```

If the test itself is wrong, continue with flow 3 or 4 before ending the session.

## 2. Build a new test step by step

```
fitnesse_debug_start(page)
fitnesse_debug_run_page(setup_only=true)
fitnesse_debug_inspect(kind='keywords')                  categories only
fitnesse_debug_inspect(kind='keywords', filter='click')  exact syntax, required rows
fitnesse_debug_inspect(kind='definedActions')
repeat: fitnesse_debug_run_wiki(step)                    keep the steps that pass
fitnesse_table_source(page) -> fitnesse_table_insert(…)  save the steps that passed
fitnesse_debug_run_page(restart=true)                    one clean run of the whole page
fitnesse_debug_end
```

## 3. Fix a table of the page

```
fitnesse_debug_page_tables                      source_page / source_index of the table
fitnesse_table_source(source_page)
fitnesse_table_save(index, original=tables[i], wiki=<new>)
                                                result.source is the page after the change:
                                                take the next original and indices from it
fitnesse_debug_run_page(tables=[k])             run it again in the same session
```

A table of an included page (SetUp, includes) is edited on that page and changes every page
that includes it.

## 4. Fix a defined action

```
fitnesse_debug_inspect(kind='definedActions', filter=…)
fitnesse_debug_defined_action_source(action)    page, index, last, original
fitnesse_table_save(page, index, last, original, wiki=<new>, reload_defined_actions=true)
                                                check reload.reloaded: false means the
                                                session still runs the old version
fitnesse_debug_run_wiki('|action|arg|')         try the new version at once
```

HTTP 409 `changed` from `defined_action_source` means the page has moved since the session
loaded it (tables inserted or removed before the definition, `----` changed): run
`fitnesse_debug_run_wiki('|define actions at|.<page>|')`, then ask again.

## 5. Go through a suite

```
fitnesse_debug_start(suite_page, suite=true)    pages: run order, kind, runnable
for each page with runnable=true:
    fitnesse_debug_run_page(page, only_failures=true)
fitnesse_debug_end                              page_totals per page
```

Moving to the next page ends the previous page's test; its tear down comes back as
`previous_page_teardown`.

## 6. When something goes wrong

| Situation | What to do |
|---|---|
| A call timed out | `fitnesse_debug_status` until `busy` is false; else `fitnesse_debug_end(abort=true)` |
| Start refused: a session runs on this page | `fitnesse_debug_status` shows it; `adopt=true, session=<id>` only if you are meant to continue it, else ask |
| Start refused: this server's session runs elsewhere | continue it, or `fitnesse_debug_end` first |
| `replaced` or `noSession` | the session is gone (someone started one on the same page, idle timeout, restart): start again and run the SetUp |
| `tooManySessions` (429) | end sessions of the account listed in `sessions`, or try later |
| Saving fails with "page changed" | `fitnesse_table_source` again, redo the change |
| `run_wiki` fails right after the start | the SetUp has not run: `run_page(setup_only=true)` |

Without a FitNesse login the session belongs to a cookie that this server keeps in memory: after a
restart of the MCP server its old session cannot be adopted and ends after the idle timeout.
