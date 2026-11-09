# Debugging with the KDTA debug tools

Typical flows for an agent (or a person driving one) that debugs FitNesse tests with this MCP
server. The tools need netcare's FitNesse fork with the `debugTest`/`debugTable` responders and
`FITNESSE_TOOLSETS` with `debug`: `read,test,debug` to run and inspect, plus `write` to change
pages. Every tool and its full description: [tools.md](tools.md).

## The tools at a glance

| Group | Tools | Mode |
|---|---|---|
| Session | `fitnesse_debug_start`, `fitnesse_debug_end`, `fitnesse_debug_status` | test (status: read) |
| Run | `fitnesse_debug_run_page` (whole page up to the first failure and on from there, chosen tables, `setup_only`, `only_failures`), `fitnesse_debug_run_wiki` (any tables); both with `with_variables`/`with_live_view` | test |
| Look | `fitnesse_debug_inspect` (`keywords`, `definedActions`, `dynamicVariables`, `scope`, `liveView`), `fitnesse_debug_page_tables`, `fitnesse_debug_read_link` (screenshot, page source of a result), `fitnesse_debug_steps` (the passed `run_wiki` steps, for saving) | read |
| Source | `fitnesse_table_source`, `fitnesse_debug_defined_action_source` | read |
| Edit | `fitnesse_table_edit` (tables: save, insert, delete, move), `fitnesse_table_edit_text` (text between tables: save, move) | write |
| Find the failure | `fitnesse_get_suite_overview`, `fitnesse_get_page_history`, `fitnesse_run_test` (not debug tools) | read / test |

What to keep in mind:

- **A session is a living test process.** Browsers, objects in scope and variables carry over
  from one call to the next until `fitnesse_debug_end`. Nothing goes into the test history. In a
  suite session they also carry over from page to page, as in a `?suite` run.
- **One object per class, or named ones.** Several objects of one class only come with
  `|add named|`, and only while the class has no global (`|add global|`) or local object yet.
  So either `add named` first: then any number of named objects, but no global or local one of
  that class any more; or global and local first: then no `add named` for it. With both a
  global and a local object, the local one comes first in the scope (`inspect(kind='scope')`)
  and wins, but it is gone at the next test, and the global one answers again.
- **SetUp first.** `fitnesse_debug_run_wiki` does not add the page's SetUp. Run
  `fitnesse_debug_run_page(setup_only=true)` right after the start, otherwise defined actions,
  the browser and fixtures are missing. As in a normal test run, the first `run_page` also runs
  the SuiteSetUp (`suite_setup` in the result), and `fitnesse_debug_end` the SuiteTearDown.
- **Trying out changes nothing.** `run_wiki` only runs tables; the `fitnesse_table_*` tools
  save the wiki page for real.
- **Two kinds of table index.** `fitnesse_debug_page_tables` counts everything a run executes
  (SetUp, TearDown, included pages); `fitnesse_table_source` counts only a page's own tables.
  `page_tables` gives each table's `source_page` and `source_index` for editing.
- **Results.** Cells are prefixed `[pass]`, `[fail]`, `[error]`, `[ignore]`; `counts` are the
  call's, `page_totals` the page's so far. Tables keep their `page_tables` index, also when
  only some ran. Inline screenshots come back as images.
- **The SetUp counts as run** after `run_page(setup_only=true)`, a whole-page run, or `tables`
  that include all `setup_tables`.
- **A test ends with its TearDown.** Moving to another page, `restart=true` and
  `fitnesse_debug_end` first run the page's TearDown tables that have not run yet (after a stop,
  or when only some tables ran; `teardown_tables` in the result), as a normal run would. Not
  after `run_wiki` alone. `skip_teardown=true` leaves them out, e.g. to look at the data a stop
  left behind.
- **A `[pass]` on a click only means the call returned.** See [Experiments and browser
  state](#experiments-and-browser-state).
- **One session per page and account** (mod #159; older forks: per account). This server works
  with one session at a time. Always end it: it holds a server slot and browsers.

## 1. Step through a failing test

The most common case: a test fails and the cause is unclear.

```
fitnesse_get_suite_overview(suite)              failed_pages with result_date
fitnesse_get_page_history(page, result_date)    the failing tables of that run
fitnesse_debug_start(page)
fitnesse_debug_run_page                         runs up to the first failing table k and stops:
                                                stopped_at (index k, source_page, source_index,
                                                text_before: the page text before it, wiki: the
                                                table's wiki), next_table; TearDown not run
                                                yet, browser stays
fitnesse_debug_read_link(html_link, filter=…)   page source at the failure ('HTML' link of the
                                                table): is the locator's id/class/text there?
                                                (the screenshot came with the stop as an image)
fitnesse_debug_inspect(kind='liveView')         optional: link for a person to watch
fitnesse_debug_run_wiki('|…variant…|')          try hypotheses: other locator, a wait
fitnesse_debug_inspect(kind='dynamicVariables') check values (or with_variables=true on the run)
fitnesse_debug_run_wiki(<stopped_at.wiki, corrected>, record=false)
                                                the console: saves nothing; until it passes
fitnesse_table_edit('save', …)                  save the version that passed, once (flow 3);
                                                no version passes: report it, save nothing
fitnesse_debug_run_page                         continue at next_table, up to the next failure
...                                             until page_done=true
fitnesse_debug_end
```

This is the debugger in the browser: run to the failure, try fixes in the console, continue.
Don't save every attempt and run it with `run_page(tables=[k])`: that changes the real page each
time. After `run_wiki` passed, the step has happened in the session, so continue without running
the saved table again. `tables=[k]` does not move `next_table` (its result repeats it while the
page's run is stopped); `from_table` continues elsewhere (e.g. after tables before it were inserted or
deleted). A test page's SuiteSetUp stops the same way: then the result has
`suite_setup.stopped_at` and the page has not begun; fix the table the same way, and the next
`run_page()` finishes the SuiteSetUp and goes on with the page. Before each new variant, bring the browser back to the state before
table k (see below). `stop_on_failure=false` runs the whole page in one go, as a normal run.

KDTA adds the 'Screen' and 'HTML' links to a failing table only when the test switched them on:
`|''save screen on error''|true|` and `|''save html on error''|true|`, usually in the SetUp, and
only for a `[fail]` or `[error]` of a browser keyword (`BrowserFixture`, `MobileAppFixture`):
`click` or `|check|title|…|` get them, `|get|2|is|1|` does not. Without them the stop has no
screenshot and there is nothing for `read_link`; look with `run_wiki` (`|show|…|`, record=false)
instead.

## 2. Build a new test step by step

```
fitnesse_debug_start(page)
fitnesse_debug_run_page(setup_only=true)
fitnesse_debug_inspect(kind='keywords')                  categories only
fitnesse_debug_inspect(kind='keywords', filter='click')  exact syntax, required rows
fitnesse_debug_inspect(kind='definedActions')
repeat: fitnesse_debug_run_wiki(step)                    passing calls are noted as steps
        fitnesse_debug_run_wiki(look, record=false)      looking around: not noted
fitnesse_debug_steps(drop=[…])                           the noted steps; drop tries that don't belong
fitnesse_table_source(page) -> fitnesse_table_edit('insert', …, wiki=steps.wiki)
                                                         save them in one go
fitnesse_debug_steps(clear=true)
fitnesse_debug_run_page(restart=true)                    one clean run of the whole page
fitnesse_debug_end
```

`run_wiki` notes every call whose tables all pass (`step` in its result); failing tries are not
noted. The steps stay after `fitnesse_debug_end`, so they can still be saved; a new
`fitnesse_debug_start` clears them. Create only pages meant for the new test.

## 3. Fix a table of the page

```
fitnesse_debug_page_tables                      source_page / source_index of the table
fitnesse_table_source(source_page)
fitnesse_table_edit('save', index, original=tables[i], wiki=<new>)
                                                result.source is the page after the change:
                                                take the next original and indices from it
fitnesse_debug_run_page(tables=[k])             only if not tried with run_wiki before: it
                                                runs the step again in the session
```

A table of an included page (SetUp, includes) is edited on that page and changes every page
that includes it.

## 4. Fix a defined action

```
fitnesse_debug_inspect(kind='definedActions', filter=…)
fitnesse_debug_defined_action_source(action)    page, index, last, original
fitnesse_table_edit('save', page, index, last, original, wiki=<new>, reload_defined_actions=true)
                                                check reload.reloaded: false means the
                                                session still runs the old version
fitnesse_debug_run_wiki('|action|arg|')         try the new version at once
```

HTTP 409 `changed` from `defined_action_source` means the page has moved since the session
loaded it (tables inserted or removed before the definition, `----` changed): run
`fitnesse_debug_run_wiki('|define actions at|.<page>|')`, then ask again. If the page no longer
has the definition (e.g. its `----` became `-----`), the fork answers 404 `unknownPlace` after the
reload and the tool says the page no longer has it; the session keeps running the old one until
it ends.

A definition ends at `----` (exactly four dashes, no dash directly before or after) anywhere in
the text between two tables, also in the middle of a line. `-----`, `------` etc. don't end one,
nor does `----` inside `!-…-!`, `{{{…}}}` or a `#` comment line.

## 5. Go through a suite

```
fitnesse_debug_start(suite_page)                suite=true, pages: run order, kind, runnable
for each page with runnable=true:
    fitnesse_debug_run_page(page)               stops at a failure: flow 1 on it,
                                                run_page() again until page_done=true
fitnesse_debug_end                              page_totals per page
```

The page's type decides between a suite and a test session; `suite=true`/`false` overrides it.
Moving to the next page ends the previous page's test: its TearDown tables that have not run
yet run first. `previous_page_teardown` reports that end (`teardown_tables`), with
`tables_not_run` if it had stopped and was not continued. For a
quick overview without stopping, `run_page(page, stop_on_failure=false, only_failures=true)`
runs the whole page and returns only the tables with `[fail]`/`[error]`.

## 6. Build a SetUp

A SetUp page is included by every test page below it. Build it like a test (flow 2), then save
the steps on the SetUp page instead of the test page.

```
fitnesse_add_child_page(suite, 'SetUp', page_type='Static')   Static: a Test page would run as a test
fitnesse_debug_start(suite.SomeTest)                           a test page below it
repeat: fitnesse_debug_run_wiki(step)                          open browser, |define actions at|, login
fitnesse_debug_steps -> fitnesse_table_edit('insert', suite.SetUp, …)  save the passed steps on the SetUp page
fitnesse_debug_run_page(restart=true, setup_only=true)         the SetUp as saved, in a fresh test
fitnesse_debug_run_page(restart=true)                          the whole page with it
fitnesse_debug_end
```

- **`restart=true` before `setup_only`.** A second `setup_only` in the same test runs the SetUp
  again (a second browser, for example); `restart=true` ends the test first, with the TearDown
  tables that have not run yet.
- **Defined actions** changed in the SetUp load again with the `|define actions at|` tables:
  `restart=true` plus `setup_only`, or `run_wiki` of that table; `inspect(kind='definedActions')`
  then shows the new version.
- **A SuiteSetUp created during a session** is unknown to it: a session remembers its SuiteSetUp
  and SuiteTearDown (single page) or its pages (suite) at the start. End it and start again.
- **Shared SetUps** affect every page below them: change only a SetUp created for the task.

## Experiments and browser state

- **Each experiment moves the browser.** After a failed table, and after every `run_wiki`
  variant, the browser is wherever that left it, so the next variant starts from another state
  than the original table. Bring it back first: run the table that navigated there again
  (`run_page(tables=[j])`), or `run_wiki` a `|get url|…|`. If unclear,
  `run_page(restart=true, tables=[0..k-1])`. Cookies and consent choices stay in the browser.
- **Check what a click did.** A click on an element that is not visible (e.g. a mobile-only
  toggle) or covered by an overlay can still give `[pass]`. After a click that matters, check its
  effect (`|element|…|visible|`, the URL, a text). A failure at the next check often belongs to
  the click before it.
- **Late overlays.** If a click passes but nothing happens, or an element is not clickable, look
  for an overlay that appeared after the page loaded (cookie consent); close it with the page's
  own keyword, then repeat the step.
- **Timing.** A cell `Timeout exceeded … actual` without `[fail]` is an `eventually becomes`
  check that timed out: optional by design and not counted, but it took the whole timeout. For a
  flaky step run the table 2–3 times in the same session; if it alternates, it is timing: wait
  for the state the next step needs.
- **Mobile browsers** (Appium) can end after about a minute without a command: don't leave such a
  session idle for long; if the browser is gone, `restart=true`.

## 7. When something goes wrong

| Situation | What to do |
|---|---|
| A call timed out | `fitnesse_debug_status` until `busy` is false; else `fitnesse_debug_end(abort=true)` |
| Start refused: a session runs on this page | `fitnesse_debug_status` shows it; it is not yours: use another page or wait for its idle timeout. Only with `FITNESSE_ALLOW_ADOPT`: `adopt=true, session=<id>` if you are meant to continue it, else ask |
| Start refused: this server's session runs elsewhere | continue it, or `fitnesse_debug_end` first |
| `replaced` or `noSession` | the session is gone: a start on the same page with the same login replaced it (e.g. a person in the wiki's debug UI), idle timeout, restart. `fitnesse_debug_status` says which in `lost_session.reason`. Start again and run the SetUp |
| `tooManySessions` (429) | end sessions of the account listed in `sessions`, or try later |
| Saving fails with "page changed" | `fitnesse_table_source` again, redo the change |
| `run_wiki` fails right after the start, with a `hint` about the SetUp | the SetUp has not run: `run_page(setup_only=true)`, then the tables again |
| You need the whole log | `fitnesse_debug_status` before `fitnesse_debug_end`: end returns only the last 30 unread entries (`log_omitted` counts the lines left out) |
| A table shows `[fail]` without a message | first `stopped_at.log` of `run_page` (warnings, errors, browser console of that table); more with `fitnesse_debug_status(log_level='WARN')` or `log_filter='BROWSER_CONSOLE'`; after a long run `log_newest=true`. The log of the test process often has the cause (e.g. `command not found`); long lines are cut at 500 characters |
| A REST call fails and the table doesn't say why | `run_wiki` `\|''set logging''\|true\|` (after `with rest`), run the call again, then `fitnesse_debug_status`: request, status, headers and response body are in the log, secrets masked as `***`. The log shows the headers the test set, not the Authorization of `set basic auth`/OAuth2, and never the request body |

Without a FitNesse login the session belongs to a cookie that this server keeps in memory: after a
restart of the MCP server its old session cannot be adopted (even with `FITNESSE_ALLOW_ADOPT`) and
ends after the idle timeout.
