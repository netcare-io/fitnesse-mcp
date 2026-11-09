# Interactive debug mode

> Copy of the fork's documentation (kdta-devcontainer `main` at b723ee0: fitnesse-netcare-mod
> `origin/master` as of #164, kind `plain` from fitlibrary-netcare-mod #88, merged after 1.6.3),
> taken 2026-11-09. It describes the contract the KDTA debug tools in [server.py](../server.py)
> build on. `AGENTS.md` and `README.md` below are the fork's files, not this repository's. Don't
> edit it here: replace it with a new copy when the fork changes.

How the debug mode of the wiki works: the responders `debugTest` and `debugTable` in fitnesse-netcare-mod, the interactive test runner
in fitlibrary-netcare-mod, the browser parts in fitlibraryweb-netcare and the UI (`debugTest.js`) in fitnesse-netcare-resources. For
starting it locally see `AGENTS.md` (Local FitNesse) and `README.md`.

## Test sessions (`?responder=debugTest`)

The responder is a JSON API with `action=start|runTable|runWiki|keywords|keywordsJson|definedActions|liveView|dynamicVariables|scope|nextPage|suitePages|pageHtml|status|log|end|abort`; the test runner is
`fitlibrary.suite.InteractiveNetcareFitLibraryServer` (override with
`${DEBUG_TEST_RUNNER}`). Each Fit protocol document must end with the counts
(`TableListener.storytestFinished()`), otherwise both sides block on read. The
keyword annotations' `wiki` texts are for display (`|''<i>with browser</i>''|`);
strip `''` and `<i>` to get runnable wiki (`|with browser|`).

### Sessions

A session belongs to its owner: the user, without authentication the browser
(cookie `debugTestSession`, which `start` sets; an API client has to send it back,
`session` alone finds nothing). An owner has any number of sessions, one per page
or suite (mod #159, merged into #157): `start` (`suite=true` for a suite session)
replaces the owner's session on the same page and leaves the others alone, so a
browser and an API client such as the FitNesse MCP with the same login don't end
each other's sessions on different pages. `start` returns the session's `session`
id; actions are meant for the session they name with `session`, without it for the
owner's session on the URL's page, else for the owner's only one (409
`sessionRequired` if several run elsewhere). A session of another owner isn't
found. `status` answers for that session (`running`, `suite`, `page`, …; `replaced`
true if a start on its page replaced it) and lists all of the owner's sessions as
`sessions` (`session`, `page`, `currentPage`, `suite`, `busy`, `startedAt`; `page` is
the page the session started on, a suite's page for a suite session, as `getFullPath()`
gives it: no leading dot, case as the page's name). Actions that name a
replaced session get 409 with `code` `replaced`. Limits: `debugTest.maxSessionsPerUser`
(5) and `debugTest.maxSessions` (20 on the server), negative for none; a start beyond
them gets 429 `tooManySessions`. The UI asks only about a session on its own page
and lists the login's sessions elsewhere (resources #188, merged into #187).
Actions that need a session
get 409 `noSession` without a running one (`end` answers 200 `{"ended":false}`). A
document that times out answers 504; the process keeps running it and further
documents get 423 until it is done (the UI polls `status` until not `busy`).

`status` with the `session` of a replaced session answers 200 with `running`
false and `replaced` true. An ended one (idle timeout, `end` or `abort`, also by
another client of the same owner, a process that died) answers `running` false
without `replaced`; the answer doesn't say why. The replaced ids are kept in
memory only (the last 1000), so after a restart of FitNesse an old id counts as
ended. Clients sharing one login (e.g. several agents on a technical account)
share `debugTest.maxSessionsPerUser` and replace each other's session on the same
page; they always send `session`, since without it `end` and `abort` may hit
another client's session.

`start` with `debug=true` starts the test process for remote debugging, with
FitNesse's `REMOTE_DEBUG_COMMAND` (`-Xrunjdwp:...,server=y,suspend=y,address=8000`):
it waits for a Java debugger on port 8000 of the FitNesse host, and FitNesse kills
it if it hasn't connected back within 60 s (`CommandRunningFitClient.TIMEOUT`,
hardcoded), so the start fails without a debugger. For a developer with an IDE,
not for API clients.

### Signals and tests

The signals the mod sends as whole documents (`DebugSession`) are the
`*_SIGNAL` constants of `InteractiveNetcareFitLibraryServer`; keep both sides
in step. `nextPage` and `pageHtml` check `page=`
with the authenticator (test/read permission), else 403. Unit tests in
`src/test/java/netcare/fitnesse/responders/debug/` mock the Fit client;
`PageTables` holds debugTable's offset logic on wiki text alone.

`<!--fitbridge:definedAction:<Base64 of the call's HTML>-->` asks the test
process which plain-text defined action the call's first row runs
(`DefinedActionCaller`, the definition loaded last wins) and where it is on its
page; it answers `definedAction | page | first | last` and one row, or none.
The call sits inside the comment, so an older lib runs nothing and answers
nothing. `DefineActionsOnPage` records the place while it defines: the index of
the header and of the last table among the page's own tables, which matches
`PageTables`; empty for a `|define action|` table.

### Pages and suites

A session is one
process; `nextPage` ends a page's storytest, and the next page keeps the
runtime of a `|suite|` fixture, as in a `?suite` run. Test results are kept per
storytest across documents, each response carries only the counts it added.

`nextPage` and `end` run FitLibrary's storytest tear down only: the `tearDown()`
of the objects that leave the scope with the storytest, as after a page's last
table; `end` also the suite tear down of a `|suite|` fixture. Neither runs the
tables of a TearDown page. A client that leaves a page before its end (after a
failure, to skip it, to restart) sends the TearDown tables itself, or what they
clean up stays in the application. Browsers don't stay on the grid:
`BrowserFixture` quits its browser at the storytest's end only with
`|shutdown browser automatically|true|` (default false), but the process's
shutdown hook quits the remaining ones when the session ends (`end`, `abort`,
idle timeout). The debug UI doesn't send them either (Skip page, Set as next table or
Run to here on another page, Restart, End).

`suitePages` lists a suite's pages in `?suite` order, `pageHtml` gives a page's HTML with
its SetUp and TearDown. For the request's own page outside a suite session it
also includes SuiteSetUp and SuiteTearDown, as the page view shows them: a client
running one page gets it all in one document, as one storytest (mod #164). A
`?test` of one page doesn't run it that way: FitNesse (`TestResponder` is a
`SuiteResponder` with one page, `PagesByTestSystem` adds the suite pages) runs
SuiteSetUp and SuiteTearDown as pages of their own, each its own storytest in the
same process, as `?suite` does; the test history of such a run has three results.
To run a page as `?test` does, ask with `withSuiteSetUp=false` and send
SuiteSetUp, the page and SuiteTearDown with `nextPage` in between (`suitePages` on
a test page lists them in that order: SuiteSetUp, the page, SuiteTearDown);
fitnesse-mcp does so. A `nextPage` to SuiteSetUp before its first table changes
nothing but the current page, which `runWiki` renders in. For another page, a
page of a suite, or in a suite session (`suite=true`) `pageHtml` leaves them out,
since a suite run sends them as pages of their own. `withSuiteSetUp=true|false`
overrides the default, the answer says it as `withSuiteSetUp`. The debug UI's
page mode takes the tables of the rendered page, so SuiteSetUp runs in the page's
storytest; its suite mode asks with the suite page as resource or in a suite
session, so it gets the pages alone.

### Keywords

The `keywords` table also lists the plain-text defined actions (kind `defined`,
column `class` the defining page, `overrides` the pages whose definitions it
overrides; `note`, `author` and `deprecated` rows of the definition as on the
help page).

`keywordsJson` returns a one-row table `keywordsJson | <keywords> | <special keywords>`
with the JSON of `|help json keywords|` (HTML-escaped): categories with `children`, per keyword `syntax`
(`%1` placeholders), `parameters` (label, `options`), `returnType` and
`tableItems`, the rows of a keyword that takes over the table; whether a row is
required is only in its `helpText` ("This statement is mandatory"). Categories and
entries come from `@JsonNetcareAction` (its `hierarchy`) and the optional
`KeywordMapping.json`/`SpecialKeywordMapping.json` in FitNesse's working directory.
Every other public method of a class in scope is in it too, with `generated: true`,
under a category named after the class's canonical name (e.g.
`netcare.fixture.browser.BrowserFixture`): annotated with a FitLibrary or netcare
keyword annotation or not at all, also the methods of a class without any
annotations; a class with `@ShowSelectedActions` gives only its annotated ones.
Their `syntax` is the annotation's `wiki` as written, markup included
(`|''<i>excel open file</i>''|%1|`), which a wiki table can't use as is (FitNesse
escapes the `<i>`); without an annotation it is the method name, e.g.
`|create order|%1|%2|`. The answer
also has that JSON parsed as `keywordsJson` (`{keywords, special}`), and
`runTable`/`runWiki` add it after the run with `withKeywordsJson=true` (mod #163;
`withKeywords=true` adds only the HTML of `keywords`).

### Live view

`liveView` returns `liveView | <hub> | <session id> | <sessions>` of the remote
browser that `BrowserFixture` registered in `fitlibrary.suite.RemoteBrowserSession`
(fitlibraryweb-netcare, both empty without one; it keeps browsers only after
`RemoteBrowserSession.enable()`, which only the interactive server calls, and
holds their fixtures weakly; its shutdown hook quits them when `abort` kills
the process): the one whose fixture was
started or `select`ed last; `<sessions>` is the JSON of all running ones
(`name` from `add named`, `hub`, `session`); the answer also has them as fields
`hub`, `seleniumSession` and `browsers` (mod #163), which the panel offers to pick from and `liveViewUrl` from
`debugTest.liveViewUrl` in `plugins.properties` as configured, with the `{session}` and `{hub}`
placeholders, which the client replaces per browser (`{hub}` is the browser's `hub` without `/wd/hub`; `hub`
is the URL the test process uses and often not reachable from outside, which is
why instances set `liveViewUrl`), `liveViewPassword` from `debugTest.liveViewPassword` and
`liveViewOnly`, true when `debugTest.liveViewOnly` is `true` or `1` (the nodes'
`SE_VNC_VIEW_ONLY`; the panel's control box is disabled then). The
Grid UI is `liveViewUrl`, else `<hub without /wd/hub>/ui/#/session/<id>`. The
debug UI's live view panel shows the VNC stream with noVNC (1.4.0, served by
FitNesse from `files/fitnesse/javascript/novnc/`, see its `README.md`) on the
WebSocket `<Grid UI URL before /ui/>/session/<id>/se/vnc`, password
`liveViewPassword`, which the server defaults to Selenium's `secret`; it replaced the
legacy popup. The panel shows up to 3 of the sessions one below the
other, each with its own noVNC connection; the others are previews, a frame
taken by a short VNC connection every 15 s, one browser at a time.

### Output

`log` (`from`, `max`) returns the test process's output from line `from` on, without
waiting for a running document: `lines` (`stream` `out`, `err` or `info` for the
command, exit code and exceptions; `text`, one line as the process wrote it;
`time`, epoch ms when FitNesse read it), `next` and `dropped`. With `max`
only the last `max` of those lines, and `skipped`, the number left out (mod #162). The
session keeps the last 5000 lines (`ExecutionLog`); `end` and `abort` return the
rest as `log` when given `logFrom` and/or `logMax` (as `from` and `max`; `logFrom`
defaults to 0), without either no log. The test process's log4j loggers write to
standard output (`log4j2.properties`, level INFO and up); slf4j and
commons-logging go through `slf4j-jdk14` to java.util.logging, which writes to
standard error (INFO and up). The debug UI's console shows it in its Output tab,
with a checkbox per stream and a dropdown with a checkbox per logger (log4j's
`%c{1}`, the short class name of a java.util.logging entry; JUL message and
stack trace lines go with the entry before them). `log` itself gives raw lines: a
message with line breaks (e.g. the REST logging's headers and bodies) is several
lines, only the first with the log4j prefix. The UI adds a line to the entry before
it on the same stream if it comes within 1 s of it (`ENTRY_MILLIS`) and doesn't
start a log4j or JUL entry (`continuedEntry` in `debugTest.js`). The `info` line
`Exit code: <n>` after `end` is the test process's: wrong plus exceptions of all
documents it ran since `start` (FitLibrary's suite counts), also of earlier runs of
a page run again in the same session; it isn't the result of the last page. The
UI's Restart ends the session and starts a new process, so its count starts at 0.

Line formats: log4j writes `[%-5level] %d{yyyy-MM-dd HH:mm:ss.SSS} [%t] %c{1} - %msg`
(`log4j2.properties` in the working directory), e.g.
`[ERROR] 2026-11-07 01:15:52.575 [BiDi Connection] BROWSER_NETWORK - [bcc6fa80] failed GET https://netcare.de/cdn/Kunden.jpg.webp: net::ERR_ABORTED`;
most FitLibrary lines have the named loggers `FITLIBRARY` and `FITLIBRARYWEB`.
java.util.logging writes with the default `SimpleFormatter` (in the JVM's locale),
two lines per entry, the source class and method, then the level and message:
`Oct 30, 2026 7:28:38 PM netcare.fitlibrary.flow.NetcareWhatIsInScope loadJsonLookUpObject`,
`WARNING: SpecialKeywordMapping.json (No such file or directory)`. `debugTest.js`
recognizes them with `LOG4J_LINE`, `JUL_LINE` and `CONTINUED_LINE`. The browser
loggers (below) write `[<first 8 characters of the Selenium session>] ` and then
`<console method>: <text>` or `uncaught: <text>` (`BROWSER_CONSOLE`; error ERROR,
warning WARN, else INFO), `<status> <method> <url> (<MIME type>)` (`BROWSER_NETWORK`,
WARN from status 400) or `failed <method> <url>: <error>` (ERROR). Their events
come in on the thread `BiDi Connection`, so a line about a table may arrive just
after the table's answer.

### Variables and scope

`dynamicVariables` returns a table `dynamicVariables | name | value | system`,
then one row per variable; `system` is `true` for the system properties
FitLibrary starts with, while unchanged (the debug UI's System properties tab).
The answer also has the rows as `variables` (`[{name, value, system}]`); a run
with `withDynamicVariables=true` adds both, the table as `dynamicVariables` and
the rows as `variables` (mod #163).

`scope` returns a table `scope | level | name | class | depth` of the scope stack
in lookup order (`level` table, selected, select, suite, global or flow; `name`
of a named select object); systems under test follow their object with `depth`
one more. Between tables only the objects that outlive a table are on it.

## Table editing (`?responder=debugTable`)

The responder needs edit permission. `action=source|save|insert|saveText|delete|move|moveText`
address a table by its index among the page's own tables, without included
pages, as the parser's offsets give them. All but `source` send the expected
old text and get 409 if the page changed in between; `save` takes exactly one
table. `source` also returns `texts`, the wiki text around the tables (`texts[i]`
in front of table `i`, one more after the last); `saveText` replaces one of them
and refuses text that would remove or change a table; tables in the new text are
added (`added`, `html`). `move` (`to`, counted before the
move) and `moveText` (`to`, `toOriginal`, `start`: in front of or after the text
there) move a table or a text in one save; `insert` and `move` take `start=true`
to go in front of the text before table `index`/`to` instead of after it. Every edit
also answers `source` (`page`, `tables`, `texts`, as `action=source`), read under the
same lock right after the save, so a client can make the next edit without reading
the page again (mod #157); the UI keeps it for its next change and loads the
page again only without it (resources #187). The debug UI uses them for editing
cells and text in place (double-click) and for drag and drop (tables and
included pages by themselves, other texts by a grip). Tables of included pages go to the
included page's path; the UI asks before the first edit of one.

`action=definedAction` (`name`: a call row, e.g. `|login|bob|as|admin|`;
`session`) asks the session with the signal above and answers `action` (the
header), `page`, `index`, `last` and `original`, as `saveBlock` on that page
takes them. Errors: 404 `notLoaded` (the call runs none) or `unknownPlace` (e.g. a
definition from `|define actions at|...|from|...|` of another root, whose pages the UI can't open),
409 `changed` with `page` (the tables `first` to `last` that the session loaded
aren't a whole definition any more: no header at `first`, another end than `last`, or
a header there that defines another action than the call, e.g. after a definition was
inserted above it; the header is compared by its keywords without formatting, case and
punctuation and by the number of arguments. Where definitions end is decided on the page
rendered by FitNesse, as the test process renders it, split as FitLibrary splits it: at a
`<hr>` or `<hr/>` between two top-level tables. So a `----` of exactly four dashes anywhere
between two tables splits; five or more don't, nor does a `----` inside `!-...-!`,
`{{{...}}}` or a `#` comment line, but one in the line after a comment does. A change inside the block that keeps its header's
keywords and arguments isn't one and answers the block as it is now;
`|define actions at|` for that page, or an edit of it with `reload=true`, gives
the places anew), 501 `unsupported` (older lib), plus the session errors 409
`noSession`/`replaced`, 423, 504. Any edit with `reload=true` and `session`
then runs `|define actions at|` for the saved page in the session's current
page and adds `reloaded`, `reloadHtml`, `reloadCounts`, `definedActions` or
`error` and `code` (`noSession`, `replaced`, `busy`, `timeout`); the keywords
table as `keywords` only with `withKeywords=true`. The page is saved either
way. `debugTest` `action=definedActions` answers the same `definedActions` for
the session: `action` (the header as runnable wiki), `page`, `overrides`,
`note`, `author`, `deprecated`, parsed from the keywords table. `deprecated` is a
boolean: FitLibrary takes a `|deprecated|` row as a mark and drops any text in it,
an explanation goes in a `|note|` row. An action
either responder doesn't know answers 400 `unknownAction` (older mods: 400
without a code). The UI's defined action editor uses `definedAction` and
`reload` and falls back to `source` and the header match without a session,
while a table runs (the session would answer only after it), and on 404, 501,
423, a 400 without a code and 409 `noSession`, `replaced` or `changed`; such a
save loads the page with `|define actions at|`, after the run if one is going on.
The editor opens from a call row's context menu (Edit > Defined action, also with
its body collapsed) and by a double-click in a call's expanded body.

## Further behaviour

- `debugTable` `action=saveBlock` replaces the tables `index` to `last` and the text between them, and refuses if the block changed; a
  defined action is several tables.
- `keywords` has rows for `@SimpleNetcareAction`, `@CompoundNetcareAction`, `@SimpleAction` and `@CompoundAction` only; methods
  with `@AnAction`, `@AnNetcareAction` or `@NullaryAction` (e.g. `add global` of `DoTraverse`) and the methods without annotation of
  an annotated class are missing there and only in `keywordsJson`.
- `keywords` also lists the public methods of plain classes without any keyword annotations (`keywordsJson` has them as well, see
  there): kind `plain` (fitlibrary-netcare-mod #88, after 1.6.3; before it `simple`, indistinguishable from an annotated keyword
  without tooltip), `wiki` the method name and its parameter types (`|''<i>create order''</i>| String | int |`, the markup in another
  order than an annotation's), `tooltip` and `author` empty, no `overrides` cell.
  Reloading a defined action from its own page replaces it instead of listing it as its own override.
- On a fail or error the row gets the links Screen and HTML only from a browser fixture (`BrowserFixture`, `MobileAppFixture`) that
  the row's keyword belongs to (`OnErrorReportHandler`), e.g. `click` or `|check|title|`; `|get|2|is|1|` gets none. If the row's
  keyword subjects are unknown, every registered fixture reports.
- The interactive server sends a keep-alive command to its browsers every 60 s while no document runs; `documentLock` keeps it out of a
  running document. The command is `getWindowHandles`: `getTitle` would dismiss an open alert.
- `KeywordMapping.json` and `SpecialKeywordMapping.json` in the test process's working directory are optional; a missing one doesn't warn.
- `|add start times|true|` in the `|runtime configuration|` adds a cell with the row's start time (`HH:mm:ss.SSS`), next to `add timings`,
  once per row; the first one of a storytest and the first after a date change have the date and the UTC offset in front.
- `BrowserEventLog` logs the browser's events over BiDi in debug sessions only, to the loggers `BROWSER_CONSOLE` and `BROWSER_NETWORK`, with
  URLs stripped of user, query, fragment and `;` path parameters, prefixed with the session (`[0123abcd] `). The debug UI's Browser tab shows
  those loggers instead of the Output tab.
- The debug UI's `fetch` calls send `X-Requested-With`: without it an expired login gets a redirect with a state cookie per request, and
  parallel ones lock the browser out at the proxy's 8 KB.
- Debug UI toolbar: without a session the Continue button reads Run and starts one. Restart runs at once and first clears what Clear
  clears. Clear (only without a session, after a confirmation) shows the tables as before the run and empties the console's Output and
  Browser tabs and the sidebar. The checkbox in Go to active follows the run: in a session it scrolls to the next table each time the
  arrow moves to another one (kept in `localStorage`).
- A cell's known values (the `options` of the keyword's parameters, e.g. the fixtures for `select`) open in a list below the cell editor
  (above it when there is more room), filtered while typing; ↑/↓, Enter or a click take one, Esc closes it; free text stays possible.
- Debug UI: A Java exception is one Output entry, up to 2000 lines and ending after its stack trace. Keywords
  concatenate by drag and drop. The keyword tree keeps frequently used keywords in `localStorage`. Defined actions can be edited and created
  from rows; one saved during a run is loaded when it has stopped. Variables filter by origin and show resolved values. The console resizes from its top edge; it, the sidebar, the live view and the Keywords panel end above the footer. FitNesse error pages
  have no breadcrumb, which causes no script error. The session dialog and notice say that another session may be someone else's with the
  same login and show its start time; their End it sends the session it showed.
- Wiki cells strip HTML tags, even inside `!-...-!`: to open a page with a script in a test, percent-encode it (`%3Cscript%3E`). A keyword
  that returns an object takes over the rest of its table (`start console logging`).

## API reference

One row per action, as `origin/master` of fitnesse-netcare-mod answers them (#164). The sections above explain the behaviour. Requests go
to `/<PagePath>?responder=debugTest|debugTable&action=<action>` (POST for long inputs such as `html` or `wiki`); a flag is on only with
the value `true`. Every answer is JSON; an error is `{"error": "...", "code": "..."}` with the HTTP status, `code` only where listed.

### Common to both responders

| Status | `code`            | When                                                                                   |
| ------ | ----------------- | -------------------------------------------------------------------------------------- |
| 400    | `unknownAction`   | Unknown `action`; also a missing required input or a number that isn't one (no `code`) |
| 403    |                   | No permission for `page` (`nextPage`, `pageHtml`, `definedAction`)                     |
| 404    |                   | The request's page or `page` doesn't exist                                             |
| 409    | `noSession`       | The action needs a running session and there is none                                   |
| 409    | `replaced`        | `session` names a session that a `start` on its page replaced                          |
| 409    | `sessionRequired` | No `session` given, none on the request's page, and the owner has several elsewhere    |
| 423    |                   | The test process still runs another document (e.g. after a 504)                        |
| 429    | `tooManySessions` | `start` beyond `debugTest.maxSessions` or `debugTest.maxSessionsPerUser`               |
| 504    |                   | The test process didn't answer within `debugTest.timeoutMinutes`; it keeps running it  |

`session` (optional on every action) names the session; without it the owner's session on the request's page, else the owner's only one.
A run's result is `html` (the document as the test process coloured it) and `counts` (`right`, `wrong`, `ignores`, `exceptions`, only
what this document added).

### `debugTest` (test permission on the request's page)

| Action             | Inputs                                                                             | Answer                                                                                                                                                 |
| ------------------ | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `start`            | `suite`, `debug` (remote debugging, see Sessions; not for API clients)             | `started`, `page`, `session`; sets cookie `debugTestSession` without authentication. Replaces the owner's session on this page                         |
| `status`           |                                                                                    | `running`; while running also `session`, `suite`, `page`, `currentPage`, `busy`, `startedAt`, `idleSecondsLeft`; `replaced`; always `sessions` (below) |
| `runTable`         | `html`; `withKeywords`, `withKeywordsJson`, `withDynamicVariables`, `withLiveView` | `html`, `counts`; with the flags `keywords`, `keywordsJson`, `dynamicVariables` + `variables`, `liveView` (a failing one is left out)                  |
| `runWiki`          | `wiki` (rendered in the context of the current page); flags as `runTable`          | as `runTable`                                                                                                                                          |
| `nextPage`         | `page` (default: the current page)                                                 | `html`, `counts` of the storytest tear down (no TearDown tables), `page`; the next documents run in `page`'s storytest                                 |
| `suitePages`       | tag filters as `?suite`                                                            | `page`, `pages` (`path`, `kind` `test`/`suiteSetUp`/`suiteTearDown`, `runnable`). No session needed                                                    |
| `pageHtml`         | `page`; `withSuiteSetUp`                                                           | `page`, `html`, `withSuiteSetUp`. No session needed                                                                                                    |
| `keywords`         |                                                                                    | `html` (keywords table), `counts`                                                                                                                      |
| `keywordsJson`     |                                                                                    | `html`, `counts`, `keywordsJson` (`keywords`, `special`)                                                                                               |
| `definedActions`   |                                                                                    | `definedActions` (`action`, `page`, `overrides`, `note`, `author`, `deprecated`)                                                                       |
| `liveView`         |                                                                                    | `html`, `counts`, `hub`, `seleniumSession`, `browsers` (`name`, `hub`, `session`), `liveViewUrl`, `liveViewPassword`, `liveViewOnly`                   |
| `dynamicVariables` |                                                                                    | `html`, `counts`, `variables` (`name`, `value`, `system`)                                                                                              |
| `scope`            |                                                                                    | `html` (table `scope \| level \| name \| class \| depth`), `counts`                                                                                    |
| `log`              | `from` (default 0), `max`                                                          | `lines` (`stream` `out`/`err`/`info`, `text`, `time`), `next`, `dropped`; with `max` `skipped`. Without a session empty, not 409                       |
| `end`              | `logFrom`, `logMax`                                                                | `ended`; `html`, `counts` of the tear down; `log` with `logFrom`/`logMax`. Without a session `{"ended": false}`                                        |
| `abort`            | `logFrom`, `logMax`                                                                | `ended`, `aborted`, `log` as `end`. No tear down                                                                                                       |

`sessions` in `status`: all running sessions of the owner, each `session`, `page`, `currentPage`, `suite`, `busy`, `startedAt` (epoch ms).
A session id unknown to the owner (another owner's, or ended) is treated as no session.

### `debugTable` (edit permission on the request's page)

`index`, `last` and `to` count the page's own tables from 0; `texts[i]` is the text in front of table `i`. `original`, `before` and
`toOriginal` are the wiki text the client last saw; a different one answers 409 (no `code`). Every edit answers `source` (as the
action) as well and takes `reload=true` (+ `session`, `withKeywords`), see below.

| Action          | Inputs                                                 | Answer                                                                                            |
| --------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `source`        |                                                        | `page`, `tables`, `texts` (one more than `tables`)                                                |
| `save`          | `index`, `original`, `wiki` (exactly one table)        | `index`, `wiki`, `html`                                                                           |
| `saveBlock`     | `index`, `last`, `original` (the block), `wiki`        | `index`, `wiki`, `html`                                                                           |
| `insert`        | `index` (up to the number of tables), `before`, `wiki` | `index`, `wiki`, `html`; `start=true` in front of the text before table `index`                   |
| `saveText`      | `index`, `original`, `wiki`                            | `index`, `text`, `added`; `html` of added tables. Changing or removing a table answers 400        |
| `delete`        | `index`, `original`                                    | `index`                                                                                           |
| `move`          | `index`, `original`, `to` (counted before the move)    | `index`, `to`; `start` as `insert`                                                                |
| `moveText`      | `index`, `original`, `to`, `toOriginal`                | `index`, `to`; `start=true` in front of the text at `to`, else after it                           |
| `definedAction` | `name` (a call row, `\|login\|bob\|`), `session`       | `action`, `page`, `index`, `last`, `original`, as `saveBlock` takes them. Changes nothing (below) |

`definedAction` errors: 404 `notLoaded`, 404 `unknownPlace`, 409 `changed` (with `page`), 501 `unsupported`, plus the session errors.

`reload=true` on an edit, after the save: `reloaded`, `reloadHtml`, `reloadCounts`, `definedActions`, with `withKeywords=true` also
`keywords`. If loading fails, `reloaded` is false with `error` and `code` (`noSession`, `replaced`, `busy`, `timeout`); the status
stays 200, as the page is saved.

Without the permission for the request's page itself (test for `debugTest`, edit for `debugTable`) FitNesse's authenticator answers
before the responder, with its own response instead of JSON.
