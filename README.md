# TodoGC

> A dead-serious **enterprise-grade cognitive memory management middleware**.
> …in plain English: it treats your todo list as a program's heap, and uses a
> garbage-collection algorithm to coldly identify and reclaim the zombie tasks
> you were never actually going to do.

![python](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/Anderson-ops-oss/TodoGC/actions/workflows/ci.yml/badge.svg)](https://github.com/Anderson-ops-oss/TodoGC/actions/workflows/ci.yml)
![deps](https://img.shields.io/badge/dependencies-0-success)
![life coverage](https://img.shields.io/badge/life%20coverage-12%25-red)
![GC throughput](https://img.shields.io/badge/GC%20throughput-98%25-success)

A normal todo app helps you **add** tasks; TodoGC helps you **honestly delete** them.

A task that stays long-term "unreachable" from your real goals (never progressed,
repeatedly deferred) gets flagged as garbage, triggering a ceremony that forces you
to pick one: **revive** it, or admit you're never doing it.

*([Optional Chinese documentation](README.zh.md))*

---

## Core Metaphor

| Memory management | Maps to your todos |
|---|---|
| Heap object | A todo item |
| GC roots | Your declared active goals |
| Reachability | Whether a task still connects to some goal |
| Reference count | How many times you actually progressed/edited it (looking doesn't count) |
| Reference cycle | Two tasks citing each other as their reason, neither reaching a goal — the sneakiest zombie |
| Generation / promotion | A deadbeat that dodges enough collection rounds gets promoted to the old generation and called out |
| Metaspace | Meta-tasks about tasks, like "be more disciplined" — resident, never collected |
| Stop-the-world | The collection ceremony: world paused, zombies handled one by one |

## Install

```bash
pip install -e .            # dev mode; installs a real `todogc` command
pip install -e ".[color]"   # colored output (rich tables + GC log highlighting)
pip install -e ".[web]"     # Web UI (Streamlit heap map + add/gc/restore interaction)
```

Core has **zero runtime dependencies** — stdlib only; `rich` (color) and `streamlit`
(Web UI) are optional enhancements — without `rich` it falls back to plain text,
without `streamlit` the CLI and tests are unaffected. Requires Python 3.10+.

## Usage

```bash
todogc add "Ship payment refactor this quarter" --root   # #1 declare an active goal (GC root)
todogc add "Write reconciliation module" --root-id 1     # #2 hang it under target #1
todogc progress 2 "core logic done"                      # real progress (+1 refcount)
todogc done 2 "Shipped"                                 # mark successfully completed; free heap capacity
todogc completed                                        # completed tasks with timestamps
todogc reopen 2                                         # return a completed task to the active heap

todogc add "Learn Rust"                                   # #3 ┐ a pair of zombies vouching for each other
todogc add "Rewrite blog in Rust"                         # #4 ┘
todogc dep 3 4                                             # #3's reason is #4
todogc dep 4 3                                             # #4's reason is #3 — a cycle

todogc ls                                    # look at your heap
todogc focus 2                               # choose an action for today
todogc today                                 # today's plan and suggestions; read-only
todogc ls -q "write" --root-id 1 --sort newest # filter a goal's entire subtree
todogc search "payment" --notes               # read-only search, including history notes
todogc tree                                  # parent/child hierarchy, including Inbox; read-only
todogc tree --show-completed                 # also show completed nodes
todogc capture "Explore a browser extension" # quickly record an idea in Inbox
todogc inbox                                 # list pending ideas and review markers; read-only
todogc heap                                  # heap map: generational layout + -Xmx usage + zombies in red (alias jmap)
todogc web                                   # move that heap map into the browser + interaction (needs [web], see below)
todogc watch                                 # scheduled checkup: push to ntfy on state transitions/heap alerts (needs [notify], see below)
todogc gc                                    # trigger collection + the Stop-the-world ceremony
todogc gc --dry-run                          # report only, skip the ceremony
todogc graveyard                             # epitaph wall: see what you've buried
todogc restore 3                             # slipped up? dig it back out of the trash
todogc show 3                                # details + leak metrics
todogc history 3                             # append-only event timeline
todogc edit 3 "Finish Rust chapter 1" --ref weak   # edit + strong/soft/weak
todogc link 3 1                              # re-link to a parent target; --off to unlink
todogc doctor                                # Cognitive Leak Detector
todogc stats --since 30d                     # GC history stats
todogc review --week this                    # weekly completion and decluttering review
todogc review --week last --export weekly.svg # share card; no optional dependencies
todogc --lang zh gc                          # optional Chinese output; English is the default
```

### What a `gc --dry-run` looks like

```
[uptime 0d00h][info][gc] GC(0) Pause Young (Allocation Failure)
  active heap    : 6 -> 3 tasks (cap 50)
  reclaimed      : 3 zombies   →  cognitive memory freed 50.0%
  reference cycle: 1 found  "Learn Rust" ⇄ "Rewrite blog in Rust"
  [Times: anxiety=0.4h  self-blame=0.2h  actual progress=0.0h]

━━ Reclaim candidates (3 items) ━━━━━━━━━━━━━━━━
[reference cycle #0] mutually vouching for each other, none reaches a root:
   ⇄ #3 Learn Rust
   ⇄ #4 Rewrite blog in Rust
[unreachable] unreachable from any active target:
   · #5 Orphan task
```

English is the default for CLI output and the Web UI, including generation names.
Chinese is an optional locale; user-authored task titles and notes retain their original language.

### What a `heap` (alias `jmap`) looks like

```
━━ Heap Memory Snapshot · jmap -heap ━━━━━━━━━━━━━━━━
  -Xmx usage: 5 / 50 obj  [██░░░░░░░░░░░░░░░░░░] 10.0%   (Metaspace excluded)

  ┌ Eden (5)
  │   ★ #1 Ship payment refactor this quarter
  │     #2 Write reconciliation module
  │   ☠ #3 Learn Rust  (cycle #0)
  │   ☠ #4 Rewrite blog in Rust  (cycle #0)
  │   ☠ #5 Orphan task  (unreachable)
  ├ Survivor (0)
  │   (empty)
  ├ Old (0)
  │   (empty)
  ├ Metaspace (1)
  │   ⊛ #6 Be more disciplined
  └──────────────
  Legend: ★ root · ☠ reclaim candidate (zombie) · ⊛ meta-task
```

With `rich` installed: zombie rows are highlighted red, `★` roots are gilded, the
`-Xmx` usage bar is colorized; `jmap` is an alias (a nod to the real JDK command
that actually prints a heap map).

### The Web UI (Streamlit) looks like this

```bash
pip install -e ".[web]"
todogc web                 # start a local server, opens in your browser (--port to change it)
```

Same core, same heap map, just moved from the terminal into the browser:

- **-Xmx usage bar** + four generation cards (Eden / Survivor / Old / Metaspace): zombie
  rows **highlighted red**, `★` root gilded, `⊛` meta-task in magenta — same color
  semantics as the CLI
- **Stop-the-world ceremony** right on the page: pick "revive / collect / skip" for each
  zombie, fill in a `finalize()` eulogy, execute with one click — runs the same
  `run_ceremony` core as the CLI (`finalize` fires once, reviving promotes the generation)
- Sidebar **add** (rejects allocation with an Allocation Failure when -Xmx is full), **graveyard**
  one-click `restore`
- **Task details** support edit/progress/snooze/link/dep and the event timeline; **Leak
  Inspector** and GC history share the same source as the CLI
- A **Language** picker at the top of the sidebar — see [Language](#language)

The Web UI is optional: without `streamlit` installed, `todogc web` just prints a friendly
install hint and never affects other commands; the browser's auto-refresh doesn't count as
"viewing" (it doesn't inflate the view counter).

## Policies, Reference Strength & Cognitive Leak Diagnosis

The project directory can hold a `todogc.toml` (full example in `todogc.toml.example`):

```toml
[gc]
mode = "normal"                 # gentle / normal / aggressive
recent_days = 30
max_consecutive_skips = 5
restore_grace_days = 7

[heap]
max = 50
```

Precedence is CLI args > environment variables > `todogc.toml` > defaults. `strong`
survives as long as it's reachable; `soft` follows the regular activity rules; `weak`
must have real progress within the time window. Progressing, editing, reviving, and
restoring all reset "consecutive skips", but the historical skip total is kept for
audit; a restored task gets a 7-day grace period by default.

The database auto-migrates when needed; a `.bak` is generated alongside
the file before migrating. A real GC run writes to the audit history; `heap` and
`gc --dry-run` are always read-only.

## Scheduled Checkup (ntfy push)

`todogc watch` means you don't have to manually run a command to check on things —
per the `[notify]`/`[watch]` config in `todogc.toml` (full fields in
`todogc.toml.example`), it polls, and pushes an [ntfy](https://ntfy.sh) notification
on state transitions:

```toml
[notify]
ntfy_enabled = true
ntfy_topic = "your-topic"        # pick something hard to guess; ntfy_token only needed for protected topics

[watch]
poll_seconds = 60                # check every minute (default)
digest_seconds = 86400           # send a heartbeat digest periodically even with no new events
remind_days = 7                  # re-remind after this many days if a candidate's state hasn't changed
heap_threshold_pct = 80          # heap usage alert threshold (%)
weekly_review_enabled = true
weekly_review_day = "friday"
weekly_review_time = "17:00"
weekly_review_timezone = "local" # or Asia/Tokyo, Europe/London, etc.
```

The push design is **event-driven**, not heartbeat noise — modeled on how a real JVM
only logs when a collection actually happens. `todogc watch` only pushes on the
following "transitions" (unchanged candidate count/state stays silent):

- A weekly review becomes due: Friday at 17:00 by default, in the configured timezone.
- A snoozed task reaches its scheduled return time (one reminder per schedule; failed delivery is retried).
- A task transitions from alive to a candidate (new zombie / new orphan / new cycle).
- A candidate's state itself changes (e.g. from stale to trapped in a cycle).
- The same candidate state has persisted too long (past `remind_days` unhandled) — the
  remind period is up, push again.
- Heap usage **newly crosses** `heap_threshold_pct` (staying high doesn't re-alert).

Reviving back to alive silently clears the dedupe record (that's good news, no push,
but next time it backslides it's treated as a "new" transition again). A heartbeat
digest is also sent every `digest_seconds`, proving the watcher is still alive so you
don't have to wonder.

`todogc watch` takes an advisory single-instance lock — if you're already running
`todogc web` (with `notify.ntfy_enabled` configured), it auto-starts the same
background thread inside the web server process; manually starting `todogc watch`
on top of that will detect the lock is held and back off, so the same transition
never gets pushed twice.

`watch` runs in the foreground and occupies the current shell. To keep adding tasks
in the **same terminal**, start it in the background with output redirected:

```bash
nohup todogc watch > todogc-watch.log 2>&1 < /dev/null &
todogc add "Plan next week" --root
jobs -l
```

Use the same database/config for both commands; use absolute paths if changing
directories. Stop the watcher with `kill` followed by its PID from `jobs -l`.
`nohup` keeps it running when the terminal closes, while the computer remains awake;
a service manager can keep it running across logins or reboots.

## Language

The default output language is **English**; Chinese is available as an opt-in, switched
via three routes, in priority order (`--lang` > environment variable > config file >
default):

```bash
todogc --lang zh gc                  # 1) CLI flag, this invocation only
TODOGC_LANG=zh todogc gc             # 2) environment variable
```

```toml
[i18n]
lang = "zh"                          # 3) persistent default in todogc.toml
```

`todogc --help`'s own text follows `--lang`/the environment variable too; but if the
language is *only* set in the config file (no `--lang`, no env var), `--help` still
shows the default language — reading the config file first would require resolving
`--config` before the language is known, which isn't possible at that point. The
actual command output, once it runs, correctly follows the config file.

The Web UI (`todogc web`) doesn't read any of the above — it has its own **per-browser-session**
language switcher (the `Language` dropdown at the top of the sidebar), seeded from
`todogc.toml`'s `[i18n] lang`, but switching only affects the current session — it's never
written back to the config file, and doesn't affect anyone else's page. The background
`todogc watch` push language only follows `[i18n] lang` (or `TODOGC_LANG`) — deliberately
decoupled from the Web UI's display language, so multiple people with the Web UI open at
once can't change each other's push language.

> English is the primary language, including reports, generation names, empty states,
> and the language picker. Chinese is available as an optional extension. Some diagnostic
> and stats labels remain in English when Chinese is selected.

## Complete and reopen tasks

Completion means the work succeeded; collection means you decided to let it go.
Both leave the active heap, but completed tasks appear in **Completed**, never the graveyard.

```bash
todogc done 2 "Published the documentation"
todogc completed                  # IDs, titles and completion times
todogc ls --state done            # completed tasks in the task-list format
todogc show 2                     # includes the completion timestamp
todogc history 2                  # retains progress, completion and reopen events
todogc reopen 2                   # reopen this task only
todogc reopen 2 --root-id 3        # optionally reassign to an active parent
```

`ls` defaults to active tasks outside Inbox; use `inbox` to list pending ideas.
Completed tasks release heap capacity, are excluded from
GC/doctor/watch checks, and cannot be progressed, edited or snoozed until reopened.
Root and meta-tasks can also be completed. Completion does not promote generations
or silently complete a parent when its last child finishes.

If a goal or parent has unfinished descendants, `done` lists them and refuses to change
anything. Complete them individually, reassign them with `link`, or explicitly finish
the whole subtree:

```bash
todogc done 1 --with-subtasks
```

The Web UI provides **Mark as done** in the task manager, an unchecked **Include unfinished
subtasks when marking done** option with a list of affected tasks, and a **Completed**
section with completion times, history and **Reopen** buttons.

Reopening retains root identity and history, resets consecutive skips and gives the
configured restore grace period (7 days by default). It does not reopen descendants.
If the old parent is completed or collected, reopen the parent first or choose another
active parent. Reopening respects the heap cap; the CLI's `reopen --force` explicitly
allows over-allocation. Completing and reopening never delete audit history.

Existing databases migrate automatically on first open, with a `.bak` created before migration.

## Weekly review and share cards

Generate a weekly review from task events, then export an English share card for sharing.
In `todogc web`, choose **Weekly review** in the sidebar, pick a week/timezone, and
optionally feature up to three completed items. Preview the card, download PNG or SVG,
or **Save snapshot** to keep the selected data and highlights unchanged.

<img src="docs/weekly-review-example.png" alt="Example TodoGC weekly review card using sample data" width="430">

*Example card with sample data.*

```bash
todogc review                               # this week so far
todogc review --week last
todogc review --week 2026-10-19 --timezone Asia/Tokyo
todogc review --week last --export weekly.svg

pip install "todogc[export]"                # optional Pillow dependency for PNG
todogc review --week last --export weekly.png
```

Weeks run from local Monday midnight through the next Monday, with the end excluded;
IANA timezones such as `Asia/Tokyo` can override the machine's local timezone. Calendar
boundaries account for daylight-saving changes. The current week stops at generation
time and is labeled **In progress**; future weeks are rejected.

- Each task is counted once per week, on its first DONE event that week. Reopening
  does not erase that finish; reopened tasks are counted separately.
- Goals and other tasks are separate totals. The daily chart includes both, so its
  total equals tasks + goals. Completing a whole subtree counts the goal and each
  completed descendant once; meta-tasks and directly completed ideas count as non-goal tasks.
- Captured and organized ideas, Inbox discards, and audited GC collections are distinct
  counts of task IDs within the period. GC dry runs never count as collections.
- **Task load now** is recorded when generating the review, even when reviewing an
  earlier week. It partitions unfinished work into active, snoozed and pending Inbox items.
- Browsing, reviewing and exporting do not write task views, progress or GC runs.
  The existing `stats` command continues to summarize GC audit history.

Cards contain numbers by default. To include titles, use actual completed IDs listed
by `review`; repeat `--highlight` up to three times. JSON includes the full review and
all completed item titles, independently of the highlights chosen for the image.

```bash
todogc review --week last --highlight 2 --highlight 5 --export weekly.png --save
todogc review --list
todogc review --snapshot 1 --export saved-week.svg
todogc review --snapshot 1 --export saved-week.json
```

Saved snapshots are stored in the current database and retain counts, names, timezone
boundaries, the recorded current task load, and selected highlights. New completion
and discard events preserve their task name, goal role and Inbox location at the time.
Older events lacking those labels fall back to current task metadata and are identified
in the review; saving a snapshot freezes the resolved values.

With ntfy configured, `watch` also sends a **Friday 17:00** review automatically.
It summarizes the calendar week containing the scheduled time, counting events
strictly before that cutoff; later Friday and weekend activity stays out of that snapshot.
The message includes completion, reopened, idea and collection counts plus a saved
snapshot ID. Titles and images are selected when you open/export the saved review.
The current task load is recorded at the actual generation time, including delayed catch-up.

On startup (including the first start), the watcher sends the most recent due review
if it has not been delivered. Missing several weeks sends only the latest due review.
A failed delivery retries the **same saved snapshot** on the next check; successful
responses are remembered across restarts. `weekly_review_enabled = false` disables
this reminder. Day, time and timezone can be overridden through the matching
`TODOGC_WATCH_WEEKLY_REVIEW_ENABLED`, `_DAY`, `_TIME` and `_TIMEZONE` environment variables
(with the full `TODOGC_WATCH_WEEKLY_REVIEW_` prefix for each). The CLI watcher and
Web UI's built-in watcher share this schedule and database state.

SVG export uses only the standard library. PNG uses Pillow and locally available fonts,
with an embedded Pillow font as fallback; it needs no browser, API key or external service.
The `[web]` extra includes PNG support. Both images are 1080 × 1350, generated from the
same layout and real counts.

## Scheduled Snooze

Snooze hides a task until a specific time. It remains unfinished, retains its parent,
root identity and generation, and still occupies heap capacity. While snoozed it is
protected from GC and excluded from leak diagnosis and candidate reminders.

```bash
todogc snooze 4 --for 2h
todogc snooze 4 --for 3d --note "Waiting for feedback"
todogc snooze 4 --until "2026-10-25 09:00"
todogc snooze 1 --for 1w --with-subtasks

todogc snoozed                      # read-only list with return times
todogc tree --show-snoozed           # include snoozed nodes
todogc unsnooze 4                   # wake this task early
todogc unsnooze 1 --with-subtasks    # also wake snoozed unfinished descendants
```

Durations use `m`, `h`, `d` or `w`; `1d` means exactly 24 hours. Absolute times use
the machine's local timezone unless an explicit UTC offset is supplied, such as
`2026-10-25T09:00+09:00`. Confirmations show the offset; schedules are stored in UTC.
Only future times are accepted. Repeating `snooze` replaces the current schedule.

Default `ls`, `inbox` and task-tree views hide snoozed tasks. A snoozed parent remains
in the tree as labeled path context when it has visible descendants; those descendants
keep their reachability. Only `--with-subtasks` snoozes unfinished descendants too.
In the Web UI, select a task or idea and open **Snooze** to choose a duration or date/time.
Use **Show snoozed tasks**, **Reschedule**, and **Wake now** to manage existing schedules.

Tasks return automatically on the next command or Web refresh after the deadline;
no background process is needed for visibility. Waking gives a 7-day GC grace period,
measured from the scheduled deadline, or from the early wake time for `unsnooze`.
Completion and discarding cancel the schedule. Snooze, wake and snoozed-list/tree
browsing do not add progress, skip counts, views or generation age. Old skip history
is retained: use `todogc defer 4 "Not now"` to record one skip without a timed pause.
The old time-less `snooze ID` form now requires `--for` or `--until`.

For active reminders, run `todogc watch` with ntfy configured, or enable the Web
watcher. It checks every **60 seconds** by default and bundles due tasks in one
notification. Successful deliveries are remembered across restarts; failed deliveries
are retried on the next check. Checks do not send a notification every minute.
Existing explicit `watch.poll_seconds` or `TODOGC_WATCH_POLL_SECONDS` settings take
precedence; change an old `1800` setting to `60` to use one-minute checks.

## Inbox

Inbox is for ideas you have captured but have not committed to a goal. It is separate
from **Unlinked**, which contains ordinary tasks that lack a parent or active goal.

```bash
todogc capture "Explore a browser extension"
todogc inbox
todogc inbox --move 7            # explicitly move an existing standalone unlinked task
```

Use the actual ID returned by `capture`. Choose one of these outcomes for a pending idea:

```bash
todogc link 7 1                  # organize under an active parent; leave Inbox
todogc root 7                    # make it a goal; leave Inbox
todogc done 7                    # finish it directly; keep completion history
todogc discard 7 "Not now"       # move it to the graveyard; recoverable
```

In the Web UI, use **Capture idea** above the tree, then select an Inbox node for
**Move task**, **Make goal**, **Mark as done**, or **Discard**. Parent pickers exclude
pending ideas: organize an idea before using it as a parent. Existing standalone
Unlinked tasks have an explicit **Move to Inbox** action; they are never moved automatically.

Ideas display time spent in Inbox and **Needs review** after 7 days. The marker appears
in the CLI list, terminal tree, and Web UI; it does not delete the idea. Inbox ideas
count toward the normal heap cap but are excluded from GC candidate selection and
leak diagnosis. The heap view includes their capacity separately from the generation rows.
Capturing, reviewing and organizing ideas do not count as progress or views.

`restore` returns a discarded idea to Inbox; `reopen` returns a completed idea to Inbox.
Both start a fresh review period and preserve the original history. Passing `--root-id`
explicitly restores/reopens it as an ordinary task under that parent instead. Restoring
and reopening respect capacity; `--force` explicitly allows over-allocation in the CLI.

## Search and filters

```bash
todogc ls -q "write"                           # active, organized, awake tasks only
todogc ls --root-id 1 --sort title --limit 10    # root #1 plus every level of descendants
todogc ls --scope inbox                        # pending, awake Inbox ideas
todogc ls --scope snoozed                      # currently sleeping tasks, including ideas
todogc search "release"                        # all states, including Done and Collected
todogc search "feedback" --notes --scope active
todogc search "guide" --scope done --sort newest
```

Keywords match a literal, case-insensitive substring of the title. `--notes` also
searches history notes; `%`, `_`, and quotes have no special meaning. Filters combine:
`--root-id` includes that node and its entire parent/child subtree, independent of
dependency/reason edges. It accepts any task ID, including a subtask.

`--scope` accepts `active`, `inbox`, `snoozed`, `done`, `collected`, or `all`.
`active` excludes Inbox ideas and sleeping tasks. Inbox shows pending, awake ideas;
use `snoozed` for sleeping ideas. The existing `--state` option remains available
as an alternative to `--scope`; `--state candidate` still computes current GC candidates.
Sort orders are `id`, `newest`, `oldest`, `title`, and `updated`; updated ignores views.
`--limit` applies after every filter and sorting.

`search` and Web browsing are read-only. `ls` records views only for results actually
displayed, after filtering and limiting. Its default range still excludes Done.
On the Web, open **Tasks → Search & filters** to find and operate on matching tasks.
Search filters are retained when navigating between pages in the same session.

## Today

```bash
todogc focus 2                  # append to today's plan
todogc focus 4 --position 1     # put #4 first; an existing selection can be reordered
todogc today                    # plan, earlier unfinished selections, and today's wake-ups
todogc progress 2 "Draft ready"
todogc done 2 "Published"
todogc unfocus 4                # remove from the plan, or dismiss an earlier suggestion
```

Today is an ordered plan for the machine's **local calendar day**, shared between
the CLI and Web UI and persisted across restarts. Choose a few actions; there is no
hard task limit. A selection is independent of Root status, and can be a goal or a
subtask. Inbox ideas must be organized first, and snoozed tasks must wake first.

After midnight a fresh plan starts. Unfinished selections from earlier days and
tasks whose Snooze ended today appear as **suggestions**; they enter the new plan
only when explicitly added. Dismissing a suggestion removes it until you select
the task again. Notification delivery does not consume wake-up suggestions.
Completed and newly unavailable selections remain visible in that day's plan so
you can see what happened or remove them. **Completed today** also includes tasks
finished outside the plan.

The Web UI lets you add tasks, move them up/down, remove them, and use the same
progress/completion/Snooze actions as task details. Planning and browsing add no
view, progress, or defer events, do not promote generations, and do not protect
tasks from GC. Record actual work with `progress` or `done`.

## Task tree

`todogc web` opens with a task tree and a detail panel. Use the arrows to expand or
collapse nested tasks; select a node to see its path, generation, history and actions.
On narrow screens the tree appears above the detail panel.

- **Inbox**, **Goals**, **Unlinked**, and **Meta-tasks** group top-level tasks. Nested tasks follow
  their parent links; dependency/reason edges do not change the hierarchy.
- **Add subtask** creates a task under the selected active task and selects it immediately.
  It follows the same heap-capacity rules as ordinary allocation.
- **Move to** lists active parents by ID and title, excluding the current task and its
  descendants. Select **No parent (unlinked)** to detach a task, then click **Move task**.
  Moving a goal retains its root identity; a linked goal is shown under its actual parent.
- **Show completed** reveals completed nodes with a check mark. Select one to view its
  completion history or reopen it. Marking a parent done still requires explicit consent
  to include unfinished descendants.
- Browsing, selecting, expanding and collapsing the tree never adds view events.

The sidebar's parent picker also uses task names, so task IDs do not need to be memorized.
Parent cycles are rejected in both the Web UI and CLI. Older cyclic parent links remain
visible with a warning and can be repaired by moving a task or unlinking it. Tasks whose
parent is unavailable are shown at the top level so they remain accessible.

The same hierarchy is available in the terminal, without recording views:

```bash
todogc tree
todogc tree --show-completed
```

```text
Task tree

Inbox
└── ○ #4 Explore a browser extension · 8d in Inbox · Needs review

Goals
└── ★ #1 Release TodoGC
    └── ○ #2 Improve the Web UI
        └── ○ #3 Write tests
```

## Aliveness Rule

A task is judged **alive** if and only if:

```
alive = linked to some active root
     && (effective activity within the last N days  ||  real progress within the last N days)
     && consecutive skips < max_consecutive_skips
```

`DEFER` and `VIEW` don't refresh effective-activity time; looking without acting, or
endlessly snoozing, can't keep a task alive. GC roots and Metaspace stay resident by
default; `doctor` separately audits whether they're generating cognitive memory leaks.

Plus one more rule: a **reference cycle** where tasks vouch for each other but none
reaches a root gets collected as a whole — this is exactly the self-deception blind
spot that reference counting can't catch and only reachability analysis can expose.
All thresholds live in `GCPolicy` — tune them until it's "a little uncomfortably honest".

## Generations

| Region | When a task enters it |
|---|---|
| Eden | Created with age 0 |
| Survivor | Revived during a GC ceremony 1–14 times |
| Old | Revived during a GC ceremony at least 15 times |
| Metaspace | Explicitly created as a meta-task; separate from the promotion sequence |

Age increases only when a candidate is revived, not when time passes or GC runs.
Completing or collecting a task removes it from the active heap; reopening retains its generation.

## Architecture

```
todogc/
  config.py     todogc.toml + env var config (stays zero-dependency even on Python 3.10)
  i18n.py       lightweight message catalog (t()/n_()) + locale resolution, a zero-dep gettext substitute
  locales/      en.py / zh.py message dicts (same key set, enforced by tests)
  heap.py       domain model (Task / Touch / generation enums)
  store.py      SQLite persistence + auto-instrumentation + aliveness metrics
  collector.py  reachability marking + Tarjan cycle detection + aliveness sweep
  report.py     JVM-style GC log + heap map (heap_view view model, shared by CLI/Web) + candidates + roast
  notify.py     ntfy push channel (JSON publish, zero-dep, plain urllib)
  watcher.py    scheduled-checkup core: event-driven + debounce + heartbeat digest + advisory single-instance lock
  tree.py       read-only task hierarchy, paths, grouping and legacy-cycle handling
  query.py      shared read-only search, ranges, subtree filtering and sorting
  today.py      daily plan, unfinished suggestions, wake-ups and completions
  cli.py        command line + Stop-the-world ceremony + todogc watch + --lang
  webapp.py     Streamlit Web UI (task tree + details + heap diagnostics + watcher + language switch, optional)
```

## Tests

```bash
pip install -e ".[dev]"
basedpyright            # Pylance/Pyright: 0 errors
ruff check todogc tests
pytest
```

## Packaging

Zero core dependencies — packs into a standard wheel/sdist for direct distribution
(`build` is already in `[dev]`):

```bash
pip install -e ".[dev]"
python -m build          # produces dist/todogc-0.10.0-py3-none-any.whl and .tar.gz
pip install dist/todogc-0.10.0-py3-none-any.whl   # install it anywhere
```

`dist/`, `build/`, `*.egg-info/` are all in `.gitignore` — build artifacts aren't
tracked. To include the Web UI: `pip install "todogc[web]"`.

## Roadmap

**Candidates for later (PRs welcome)**: GitHub Issues/PR sync, TODO/FIXME import,
interactive reference graph, finishing optional Chinese translations of diagnostic/stat labels.

## License

MIT
