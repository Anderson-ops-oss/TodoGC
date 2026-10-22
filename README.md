# TodoGC

A local task manager that treats unfinished work as a memory heap: organize tasks
under real goals, plan today's work, and use garbage collection to review tasks
that have gone stale or lost their purpose.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/Anderson-ops-oss/TodoGC/actions/workflows/ci.yml/badge.svg)](https://github.com/Anderson-ops-oss/TodoGC/actions/workflows/ci.yml)
![Core dependencies](https://img.shields.io/badge/core%20dependencies-0-success)

TodoGC includes a terminal CLI, an optional local Web UI, Inbox capture, nested
subtasks, daily planning, scheduled Snooze, completion history, weekly share cards,
and optional ntfy reminders. Tasks live in a local SQLite database. English is the
primary language; [Chinese documentation](https://github.com/Anderson-ops-oss/TodoGC/blob/main/README.zh.md) and output are optional.

## Install and quick start

Requires **Python 3.10+**. Once a release is published on PyPI, install it without
cloning the repository:

```bash
python -m pip install todogc
```

For a standalone command without manually creating or activating an environment,
use `pipx install todogc` if you have [pipx](https://pipx.pypa.io/stable/installation/)
installed. It manages an isolated environment and exposes the `todogc` command.
Source installation is covered under [Development](#development).

The core uses only the Python standard library. Optional features:

| Feature | Install from PyPI |
|---|---|
| Colored terminal output | `python -m pip install "todogc[color]"` |
| Web UI and PNG exports | `python -m pip install "todogc[web]"` |
| PNG exports without the Web UI | `python -m pip install "todogc[export]"` |

Upgrade with `python -m pip install --upgrade todogc`, or `pipx upgrade todogc`
for a pipx installation. Use `pipx install "todogc[web]"` when choosing the Web extra
for a new pipx installation.

Try a goal and two levels of subtasks:

```bash
todogc add "Release TodoGC" --root            # #1 in a new database
todogc add "Write the user guide" --root-id 1 # #2
todogc add "Check the examples" --root-id 2   # #3
todogc focus 3
todogc today
todogc progress 3 "Checked the quick start"
todogc done 3 "Examples verified"
todogc tree --show-completed
```

Use the actual IDs returned by your commands. Examples throughout this guide are
individual operations, rather than one script to run against an existing database.
No configuration file or notification service is needed for ordinary task management.

### Install the official AI skill with one prompt

Give Codex or Claude Code this prompt:

> Install or upgrade TodoGC from PyPI, then install its bundled official Skill for this agent using `todogc skill install --target codex` for Codex or `todogc skill install --target claude-code` for Claude Code; preserve my existing task database and verify the installation with `todogc skill status`.

The official English skill ships **inside the wheel and source package** starting
with 0.13.0. It needs no GitHub access, MCP server, hooks, API key, or source checkout.
The agent can use pipx or its available Python environment, then run the installer.
If the skill is not available in the current session, start a new session or have
the agent read the installed `SKILL.md` directly for that conversation.

Manual installation, after installing/upgrading the Python package:

```bash
todogc skill install --target codex
todogc skill install --target claude-code
todogc skill install --target all
todogc skill status --json
```

| Platform | Default personal skill directory | Manual invocation |
|---|---|---|
| Codex | `~/.agents/skills/todogc/` | `$todogc` (CLI/IDE), or select the skill in your client |
| Claude Code | `~/.claude/skills/todogc/` | `/todogc` |

For example, ask `$todogc Capture an idea: explore an IBKR trading system`, or
`/todogc Fix task #7's title to "Finish the IBKR trading system"`. The agent uses
the existing task commands and returns actual task IDs/results. The skill also
supports Today, progress/completion, Snooze, task trees, and weekly reviews.

Installation records the current Python executable and absolute database/config
paths in `runtime.json` beside the skill. Explicit `--db` / `TODOGC_DB` wins.
A newly added platform reuses an existing managed skill's bindings. Otherwise,
an existing `todogc.db` in the current directory is reused, or
`~/.todogc/todogc.db` is selected. Configuration follows `--config` /
`TODOGC_CONFIG`, an existing local `todogc.toml`, or `~/.todogc/todogc.toml`.
The installer does not create or migrate the database or copy credentials.
Reinstalling/upgrading preserves existing bindings unless explicitly overridden:

```bash
todogc --db /absolute/path/tasks.db --config /absolute/path/todogc.toml skill install --target all
```

Use those same paths with the CLI/Web UI to open the skill's task list. Ordinary
CLI defaults still use the current directory. The skill creates the data directory
when needed for the first task, rather than silently switching lists across projects.
`CLAUDE_CONFIG_DIR` is honored for Claude Code. `--directory PATH` overrides the
exact skill directory for a single target. Managed, unedited files update in place;
local edits are preserved unless `--force` is explicitly supplied, and unrelated
files are retained. Upgrading the Python package alone does not refresh copied
skills: rerun the skill installer. This is a local-agent integration; cloud sessions
need access to the installed skill, Python environment, and database separately.

## Usage guide

All command examples and their behavior are grouped here:

| I want to… | Go to |
|---|---|
| Choose a database, config file, or language | [Data and configuration](#data-and-configuration) |
| Install the Codex / Claude Code skill | [Official AI skill](#install-the-official-ai-skill-with-one-prompt) |
| Add, edit, finish, or reopen work | [Task lifecycle](#task-lifecycle) |
| Organize goals and nested subtasks | [Tree and relationships](#tree-and-relationships) |
| Capture an idea before committing to it | [Inbox](#inbox) |
| Decide what to do today | [Today](#today) |
| Find tasks across different states | [Search and filters](#search-and-filters) |
| Pause a task until a specific time | [Snooze and defer](#snooze-and-defer) |
| Inspect stale work, collect it, or recover it | [GC and recovery](#gc-and-recovery) |
| View completion statistics or export a card | [Weekly reviews](#weekly-reviews) |
| Configure reminders and manage the background process | [Watch and notifications](#watch-and-notifications) |
| Work in the browser | [Web UI](#web-ui) |

### Data and configuration

By default, TodoGC uses **`todogc.db` and `todogc.toml` in the current directory**.
The database is created on first use; the config file is optional. The complete
configuration template is [todogc.toml.example](https://github.com/Anderson-ops-oss/TodoGC/blob/main/todogc.toml.example).

Global options go **before** the command:

```bash
todogc --db /absolute/path/tasks.db today
todogc --config /absolute/path/todogc.toml --db /absolute/path/tasks.db watch status
todogc --heap-max 100 --gc-mode gentle gc --dry-run
```

Use the same database path when switching directories or managing a watcher.
`--config` selects configuration; it does not select a database. To keep paths
consistent across terminal sessions, set `TODOGC_DB` and `TODOGC_CONFIG` in your shell.

Configuration precedence is **CLI options > environment variables > config file >
defaults**. Common settings are:

| TOML setting | Default | Environment override |
|---|---|---|
| `[heap] max` | `50` | `TODOGC_HEAP_MAX` |
| `[gc] mode` | `"normal"` | `TODOGC_GC_MODE` |
| `[gc] recent_days` | `30` | `TODOGC_RECENT_DAYS` |
| `[gc] max_consecutive_skips` | `5` | `TODOGC_MAX_SKIPS` |
| `[gc] restore_grace_days` | `7` | `TODOGC_RESTORE_GRACE_DAYS` |
| `[i18n] lang` | `"en"` | `TODOGC_LANG` |

English needs no language flag. For optional Chinese output, use `--lang zh`,
`TODOGC_LANG=zh`, or `[i18n] lang = "zh"`. The Web UI has a per-session language
picker, initially seeded from configuration. Watch notifications use the configured
language or `TODOGC_LANG`; changing the Web picker or CLI `--lang` does not change
notification language. Help text follows `--lang` or `TODOGC_LANG`, but does not read
the config file's language before showing help. User titles and notes are never translated.

Databases migrate automatically when opened by a command that uses the task store;
a `.bak` is created before migration. Watch status and logs do not create or migrate
a database. Keep your database and local notification credentials out of commits.

### Task lifecycle

```bash
todogc add "Publish the guide" --root-id 1
todogc progress 2 "First draft ready"
todogc edit 2 "Publish the revised guide"
todogc edit 2 --ref weak
todogc show 2
todogc history 2 --limit 10
todogc done 2 "Published"
todogc completed
todogc reopen 2
todogc reopen 2 --root-id 1
```

| Command | Meaning |
|---|---|
| `add` | Create a task; the Web UI calls this **Allocate task**. |
| `progress` | Record work and an optional note. It adds one progress event without finishing the task. |
| `edit` | Change the title or reference strength; an edit also counts toward progress metrics. |
| `show` | Inspect current state, parent, generation, progress, views, and skips. |
| `history` | Read the append-only event timeline and notes. |
| `done` | Mark work successfully completed and release its heap capacity. |
| `completed` | List completed tasks and completion times. |
| `reopen` | Return a completed task to unfinished work, preserving its history. |

Completed tasks are excluded from default lists and GC checks. Reopen them before
editing, progressing, or snoozing them. Root and meta-tasks can also be completed.
Completion and collection are separate outcomes: completed work belongs in
**Completed**, while abandoned/collected work belongs in the **graveyard**.

A parent with unfinished descendants cannot be completed on its own. Finish or
move those descendants first, or explicitly finish the entire subtree:

```bash
todogc done 1 --with-subtasks
```

This includes unfinished descendants at every depth. Finishing the last child does
not automatically finish its parent. Reopening affects one task only, preserves its
root identity and generation, and grants the configured restore grace period.
If its parent is unavailable, reopen that parent first or pass another `--root-id`.

The heap cap defaults to 50 unfinished non-meta tasks, including Inbox and Snooze.
`add`, `capture`, `reopen`, and `restore` accept `--force` to explicitly exceed it
(**Force over-allocate** in the Web UI). Completing or collecting work frees capacity.

### Tree and relationships

```bash
todogc add "Ship the release" --root
todogc add "Prepare documentation" --root-id 1
todogc add "Proofread examples" --root-id 2
todogc tree
todogc tree --show-completed --show-snoozed
todogc link 3 1                      # move #3 under #1
todogc link 3 --off                  # remove its parent link
todogc root 2                       # make #2 an active goal
todogc root 2 --off                  # remove goal status
```

`--root` declares an **active goal / GC root**. Despite its name, `--root-id` chooses
an immediate parent: a child can have children of its own. Parent cycles are rejected.
You can also add a task with neither option; it appears under **Unlinked** and may
be a GC candidate because it has no path from an active goal. Use `capture` instead
when the item is an idea you have not yet organized.

The tree follows parent links and groups top-level tasks into Inbox, Goals, Unlinked,
and Meta-tasks. Completed and snoozed nodes are hidden unless requested; hidden
parents may appear as context for visible children. Tree browsing never adds views.

Two advanced relationships are independent of the task hierarchy:

```bash
todogc dep 2 3                      # add a reason/reference edge from #2 to #3
todogc dep 2 3 --remove
todogc add "Review how I plan work" --meta
```

`dep` adds a reference used by GC reachability and cycle detection. It does not move
a task in the tree, select it for Today, or enforce an execution order. Meta-tasks
live in **Metaspace**, outside the normal capacity cap and ordinary GC collection.
See [How GC works](#how-gc-works) for reference strengths and generations.

### Inbox

Capture ideas without treating them as committed goals:

```bash
todogc capture "Explore a browser extension"
todogc inbox
todogc inbox --move 7                # move an existing standalone unlinked task
```

Choose one outcome for the idea, using its actual ID:

```bash
todogc link 7 1                      # organize under a parent; leave Inbox
todogc root 7                        # turn it into a goal; leave Inbox
todogc done 7                        # complete the idea directly
todogc discard 7 "Not pursuing it"   # send it to the graveyard
```

Pending ideas occupy capacity but are excluded from GC and leak diagnosis. They
show **Needs review** after 7 days; this marker does not delete them. Organize an
idea before using it as a parent or selecting it for Today. Ordinary Unlinked tasks
are never moved into Inbox automatically.

`restore` returns a discarded idea to Inbox; `reopen` returns a completed idea there.
Both preserve history and start a fresh review period. Passing `--root-id` instead
returns it as an ordinary task under that parent. Capture, Inbox browsing, and
organization do not add views or progress credit.

### Today

```bash
todogc focus 2                              # append one task to today's plan
todogc focus 1 --with-subtasks              # include available descendants
todogc focus 4 --position 1                 # place or move #4 first
todogc focus 1 --with-subtasks --position 1  # move the available branch together
todogc today
todogc unfocus 4                            # remove a selection or dismiss a suggestion
```

Today is an ordered plan for the machine's **local calendar day**, persisted in the
database and shared with the Web UI. Goals and subtasks can both be selected; Inbox
ideas must be organized first, and snoozed tasks must wake first.

`focus ID` selects only that task. `--with-subtasks` follows parent links through
all levels, skipping Done, Collected, Inbox, and currently snoozed descendants. Awake
children of a sleeping parent can still be selected. The starting task must itself
be available. Reason/reference edges are not followed. New selections append in
parent-before-child order; existing ones keep their positions without duplicates.
With `--position N`, the available branch moves as one block among the remaining selections.

After midnight, a fresh plan starts. Earlier unfinished selections and tasks whose
Snooze ended today appear as **suggestions**, and require an explicit `focus` to join
the plan. `unfocus` dismisses a suggestion until you select that task again.
Completed or newly unavailable selections remain visible in that day's plan;
**Completed today** also includes work finished outside the plan.

Planning does not add views, progress, skips, generation age, or GC protection.
Use `progress` and `done` to record actual work.

### Search and filters

```bash
todogc ls
todogc ls -q "write" --root-id 1 --sort newest --limit 10
todogc ls --scope inbox
todogc ls --scope snoozed
todogc ls --state done
todogc ls --state candidate
todogc search "release"
todogc search "feedback" --notes --scope active
todogc search "guide" --scope done --sort title
```

`ls` defaults to unfinished, awake tasks outside Inbox. `search` defaults to **all
states**, including Done and Collected. Both share these filters:

| Option | Behavior |
|---|---|
| `-q TEXT` / `--search TEXT` | Title query for `ls`; `search` takes its query as a positional argument. |
| `--notes` | Also match history notes. |
| `--root-id ID` | Include this task and its entire parent/child subtree, at any depth. |
| `--scope` | `active`, `inbox`, `snoozed`, `done`, `collected`, or `all`. |
| `--state` | Alternative to scope: `live`, `candidate`, `done`, or `collected`; `candidate` computes current GC candidates. |
| `--sort` | `id` (default), `newest`, `oldest`, `title`, or `updated`. Updated ignores view events. |
| `--limit N` | Limit results after filtering and sorting. |

Queries are literal, case-insensitive substrings. `%`, `_`, and quotes have no
wildcard meaning. `active` excludes Inbox and sleeping tasks; `inbox` shows awake
pending ideas, while `snoozed` includes sleeping ideas. Choose `--scope` or `--state`.

`search` is read-only. Ordinary `ls` records views only for the displayed results;
its default list never adds views to completed tasks. The computed candidate list,
`show`, `history`, `tree`, `today`, and Web browsing do not add views. Views never
count as progress or keep a task alive.

### Snooze and defer

```bash
todogc snooze 4 --for 2h
todogc snooze 4 --for 3d --note "Waiting for feedback"
todogc snooze 4 --until "2030-10-25 09:00"
todogc snooze 1 --for 1w --with-subtasks
todogc snoozed
todogc unsnooze 4
todogc unsnooze 1 --with-subtasks
todogc defer 4 "Postponed without a return date"
```

Snooze hides unfinished work until a future time. It retains parent links, root
identity, generation, and capacity usage, while protecting it from GC and leak
checks. It does not add a skip or progress event. Use `--with-subtasks` to also pause
unfinished descendants; otherwise children keep their own schedules.

Durations accept `m`, `h`, `d`, or `w`; `1d` is exactly 24 hours. Absolute times use
the machine's local timezone unless an offset is supplied, such as
`2030-10-25T09:00+09:00`; they are stored in UTC. Replace example dates with a future
time. Repeating `snooze` reschedules; `unsnooze` wakes early.

Tasks become visible on the next command or Web refresh after their deadline,
without a background process. Wake-up grants a fixed 7-day GC grace period from
the deadline, or from an early `unsnooze`. Completion and discard cancel the schedule.
For a pushed reminder, configure [Watch](#watch-and-notifications).

`defer` records one skip without scheduling or hiding the task. Repeated deferrals
can make a task a GC candidate. Use `progress` when work actually moved forward.

### GC and recovery

| Command | What it tells you | Changes task state? |
|---|---|---|
| `heap` / `jmap` | Generation layout, capacity usage, and candidate markers. | No |
| `doctor` | Cognitive leak diagnostics, including resident goals and meta-tasks. | No |
| `gc --dry-run` | GC report and the tasks proposed for collection. | No |
| `gc` | The same report, then an interactive decision for each candidate. | When you choose an action |
| `stats --since 30d` | Recorded GC runs and outcomes from the last 30 days. | No |

```bash
todogc heap
todogc doctor
todogc gc --dry-run
todogc gc --mode gentle
todogc stats --since 30d
todogc graveyard
todogc restore 4 --root-id 1
```

The collection ceremony offers **revive**, **collect**, or **skip**. Revive keeps a
candidate and increases its generation age. Collect moves it into the graveyard and
lets you leave a note. Skip leaves it unresolved. A real GC run records an audit
entry; dry runs do not. The watcher only checks and sends reminders; it does not
run the interactive ceremony or collect tasks for you.

`graveyard` lists collected/discarded work and its notes. `restore 4 --root-id 1`
returns task #4 from the graveyard under active parent #1. Restoring preserves
history and gives the configured grace period. Use `reopen` for completed work.
`gc --full` changes the report's pause label; it uses the same collection rules.

### Weekly reviews

`review` summarizes successful work and decluttering; `stats` summarizes GC audits.

```bash
todogc review                               # this week so far
todogc review --week last
todogc review --week 2026-10-19 --timezone Asia/Tokyo
todogc review --week last --export weekly.svg
todogc review --week last --export weekly.png
todogc review --week last --highlight 2 --highlight 5 --export weekly.png --save
todogc review --list
todogc review --snapshot 1 --export saved-week.svg
todogc review --snapshot 1 --export saved-week.json
```

SVG needs no dependencies. PNG needs the `[export]` extra or `[web]`; both formats
use the same 1080 × 1350 layout and are generated locally. Cards are in English and
show numbers by default. `--highlight` adds titles for up to three completed IDs
listed by that review. JSON includes the full review and all completed item titles.

<img src="https://raw.githubusercontent.com/Anderson-ops-oss/TodoGC/main/docs/weekly-review-example.png" alt="TodoGC weekly review card with sample data" width="430">

*Example card using sample data.*

Weeks run from Monday midnight to the next Monday, excluding the end. The local
timezone is the default; `--timezone` accepts an IANA name. The current week stops
at generation time and is labeled **In progress**; future weeks are rejected.

- Each task counts once, on its first completion that week. Reopening is counted
  separately and does not erase the finish. Goals and other tasks have separate totals.
- A completed subtree counts the goal and each finished descendant individually.
  Meta-tasks and directly completed ideas count as non-goal tasks.
- Captured/organized ideas, Inbox discards, and actual GC collections are separate
  counts, deduplicated by task ID within the period. Dry runs do not count as collections.
- **Task load now** describes active, snoozed, and pending Inbox work at generation
  time, even when the review covers an earlier week.

`--save` freezes counts, titles, highlights, timezone boundaries, and current task
load in the database. Open saved versions with `--list` and `--snapshot ID`. New
events preserve task labels at the time; older events without labels use current
metadata and are identified in the review. A saved snapshot freezes those resolved values.
Browsing and exporting do not add task views, progress, or GC runs.

### Watch and notifications

Watch checks for reminders every **60 seconds** by default and pushes through ntfy.
**Notifications must be configured before `watch`, `watch start`, or `watch test`.**
Normal task management and Snooze visibility work without them.

**1. Create `todogc.toml` in your working directory if it does not exist.**
Use the contents below, or download the linked configuration template. A pip/pipx
installation does not require a repository checkout.

**2. Add or replace the `[notify]` and `[watch]` sections:**

```toml
[notify]
ntfy_enabled = true
ntfy_server = "https://ntfy.sh"
ntfy_topic = "replace-with-your-own-hard-to-guess-topic"
# ntfy_token = "..."             # for protected topics; prefer TODOGC_NTFY_TOKEN

[watch]
poll_seconds = 60
digest_seconds = 86400
remind_days = 7
heap_threshold_pct = 80
lock_stale_seconds = 300
weekly_review_enabled = true
weekly_review_day = "friday"
weekly_review_time = "17:00"
weekly_review_timezone = "local" # or Asia/Tokyo, Europe/London, etc.
```

Subscribe to that same topic on the same ntfy server in your receiving client.
Copying the template alone does not enable push: its defaults are disabled with an
empty topic. Keep tokens out of Git; `TODOGC_NTFY_TOKEN` overrides the file setting.

**3. Test delivery, then start the background watcher:**

```bash
todogc watch test
todogc watch start
todogc watch status
todogc add "Plan next week" --root  # the same terminal remains usable
todogc watch logs --lines 50
todogc watch stop
```

| Command | Behavior |
|---|---|
| `watch` | Run in the foreground; Ctrl-C stops it. |
| `watch start` | Run detached; report the existing watcher if one already owns this database. |
| `watch status` | Show owner, mode, PID, heartbeat, checks, delivery results, and weekly schedule. Add `--json` for scripts. |
| `watch test` | Send one generic notification. It does not inspect tasks, acknowledge Snooze reminders, or generate a review. |
| `watch logs --lines N` | Read recent logs, including rotated files. |
| `watch stop` | Request a graceful stop; a send already in progress may finish first. |

A successful test means the server accepted the message; check your receiving
client too. If you see **Scheduled push not enabled**, check the current directory,
`--config` / `TODOGC_CONFIG`, `ntfy_enabled = true`, and a non-empty `ntfy_topic`.
For delivery failures, inspect `watch status` and `watch logs` and verify the server,
topic, and any required token.

The watcher sends:

- Due Snooze reminders, bundled per check; failed delivery retries on the next check.
- New or changed GC candidates, and reminders after `remind_days` of the same state.
- A capacity alert when usage crosses `heap_threshold_pct`.
- A heartbeat digest every `digest_seconds`, even without new events.
- A weekly review at the configured time, **Friday 17:00** by default.

One-minute checks do not mean one-minute pushes. Weekly notifications contain
numeric summaries and a saved snapshot ID; they do not attach an image or task
titles. Open that snapshot to choose highlights and export a card.
The scheduled review covers its calendar week strictly before the scheduled cutoff;
late-Friday and weekend events are outside that snapshot. Current task load is
recorded at actual generation time.

On startup, including the first start, Watch catches up the latest due undelivered
review. After several missed weeks it sends only the latest one. Failed sends retry
the same saved snapshot; successful delivery is remembered across restarts. Set
`weekly_review_enabled = false` to disable weekly push. Overrides use the
`TODOGC_WATCH_WEEKLY_REVIEW_` prefix plus `ENABLED`, `DAY`, `TIME`, or `TIMEZONE`.

Foreground, background, and Web watchers share one owner per database. Stopping a
Web watcher stops its check thread while leaving the Web server available; resume
with `watch start` or restart the Web server. Background settings are captured at
startup, so apply changes with `watch stop` followed by `watch start`. Status labels
whether it is showing running, last-run, or current settings.

Logs live next to the database at `<database>.watch/watch.log`, capped at 1 MiB per
file with three backups. Saved status and logs omit ntfy credentials. Status, logs,
and stop remain usable with a broken config file. The detached process can continue
after the terminal closes while the computer is awake; checks pause while it sleeps.
**After a reboot, start it again. Login/startup integration is not included.**

### Web UI

After installing `[web]`:

```bash
todogc web
todogc web --port 8502
```

The browser uses the same database and core rules as the CLI. The sidebar groups work into:

| Page | What you can do |
|---|---|
| **Today** (default) | Select and order today's actions, view suggestions, record progress, and finish work. |
| **Tasks** | Browse the tree, search/filter, allocate tasks, inspect details, move tasks, and manage completed work. |
| **Inbox** | Capture, organize, snooze, complete, discard, and recover ideas. |
| **Weekly review** | Choose a week/timezone, preview cards, select highlights, download, and save snapshots. |
| **Maintenance** | Inspect the heap, run the GC ceremony, diagnose leaks, read GC history, and restore collected work. |

In task forms, **Allocate task** means create; **Active root** declares a goal;
**Meta-task** uses Metaspace; **Reference strength** selects the retention rule;
**Force over-allocate** allows exceeding capacity. **Mark as done** completes work,
and including unfinished subtasks requires an explicit checkbox. The task detail
panel also provides progress, edit, Snooze, parent/reference links, and history.

Tree expansion and page refresh do not add views. Search filters persist when
switching pages within a session. If notifications are enabled, the Web server
starts the same watcher in a thread, sharing ownership with terminal watchers.
Without the optional dependency, `todogc web` prints an installation hint.

## How GC works

GC identifies **candidates for your review**. Roots are your active goals; ordinary
tasks need to be reachable from them through parent or reference links.
Inbox, sleeping tasks, and recently restored/reopened/revived or awakened work have
separate protection rules described in the guide. Roots and meta-tasks stay resident
by default; `doctor` audits them separately.

For an ordinary reachable task outside a grace period:

| Reference strength | Retention rule |
|---|---|
| `strong` | Reachability is enough. |
| `soft` (default) | Recent effective activity or progress, and fewer than the skip threshold. |
| `weak` | Recent progress/edit activity, and fewer than the skip threshold. |

Views and deferrals do not refresh effective activity. With the default configuration:

| GC mode | Activity window | Consecutive-skip threshold |
|---|---|---|
| `gentle` | 60 days | 7 |
| `normal` | 30 days | 5 |
| `aggressive` | 15 days | 3 |

Modes adjust the configured baseline: gentle doubles the window and adds two skips;
aggressive halves the window and subtracts two, with a minimum of one. Reaching
the threshold makes the activity check fail. Strong references still require
reachability; a group of references disconnected from every goal can become a
cycle candidate. Progress, editing, revival, and restoration reset consecutive
skips while keeping the historical total.

Generations describe how many times you have chosen to keep a candidate:

| Region | Entry rule |
|---|---|
| **Eden** | Newly created, age 0. |
| **Survivor** | Revived during the GC ceremony 1–14 times. |
| **Old** | Revived at least 15 times. |
| **Metaspace** | Explicit meta-task, outside the regular promotion sequence. |

Age increases only on ceremony revival, rather than with elapsed time or each GC
scan. Completion/collection removes work from the active heap; reopening retains
its generation.

## Development

For source installation and development, run these commands from the checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check todogc tests scripts
basedpyright
pytest
python -m build
python -m twine check --strict dist/*
```

The activation command is for macOS/Linux; in Windows PowerShell use
`.venv\Scripts\Activate.ps1`. For editable optional features, install
`python -m pip install -e ".[web]"` or the matching extra.

Build output goes to `dist/` as a wheel and source archive. Core dependencies remain
empty; `rich`, Streamlit, and Pillow are optional extras. Run
`python scripts/smoke_wheel.py /path/to/todogc.whl` to install a single built wheel in
a temporary environment and exercise its commands outside the checkout.

### Publishing

[publish.yml](https://github.com/Anderson-ops-oss/TodoGC/blob/main/.github/workflows/publish.yml)
uses PyPI Trusted Publishing with the following registration:

| PyPI field | Value |
|---|---|
| Project | `todogc` |
| Owner | `Anderson-ops-oss` |
| Repository | `TodoGC` |
| Workflow | `publish.yml` |
| Environment | Leave blank; the workflow does not declare an environment. |

No stored PyPI API token is needed. Running **Publish to PyPI → Run workflow** in
GitHub Actions runs tests, builds, checks metadata, and tests the installed wheel,
without uploading. Ordinary pushes do not publish either.

To publish, make sure `pyproject.toml` and `todogc.__version__` match, commit the
release changes to `main`, then publish a non-prerelease GitHub Release with tag
`v<version>`. The workflow checks the version and that the tagged commit is on
`main`, runs tests on Python 3.10–3.13, builds and checks the distributions, and
uploads the verified artifacts through OIDC. Only the upload job can request an
identity token. Every later release needs a new version; PyPI files cannot be replaced.

| Modules | Responsibility |
|---|---|
| `heap.py`, `store.py` | Domain model, SQLite storage, event history, and migrations. |
| `collector.py`, `report.py` | Reachability, cycles, GC policies, and diagnostic output. |
| `tree.py`, `query.py`, `today.py` | Shared hierarchy, search, and daily planning. |
| `review.py`, `review_card.py` | Weekly statistics, snapshots, and exports. |
| `notify.py`, `watcher.py`, `watch_service.py`, `scheduling.py` | Delivery, checks, process management, and scheduling. |
| `config.py`, `i18n.py`, `locales/` | Configuration and English/Chinese messages. |
| `skill_install.py`, `skills/todogc/` | Offline skill installation, fixed runtime bindings, and shared agent guidance. |
| `cli.py`, `webapp.py` | Terminal and optional browser interfaces. |

## License

MIT
