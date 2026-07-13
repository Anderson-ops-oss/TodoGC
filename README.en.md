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
repeatedly snoozed) gets flagged as garbage, triggering a ceremony that forces you
to pick one: **revive** it, or admit you're never doing it.

*([中文说明见 README.md](README.md))*

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

todogc add "Learn Rust"                                   # #3 ┐ a pair of zombies vouching for each other
todogc add "Rewrite blog in Rust"                         # #4 ┘
todogc dep 3 4                                             # #3's reason is #4
todogc dep 4 3                                             # #4's reason is #3 — a cycle

todogc ls                                    # look at your heap
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
todogc --lang zh gc                          # switch output language to Chinese (English is the default, see v0.4 below)
```

### What a `gc --dry-run` looks like

```
[运行 0d00h][info][gc] GC(0) Pause Young (Allocation Failure)
  活跃堆         : 6 -> 3 tasks (上限 50)
  本轮回收       : 3 zombies   →  释放认知内存 50.0%
  reference cycle: 1 处  「Learn Rust」 ⇄ 「Rewrite blog in Rust」
  [Times: 焦虑=0.4h  自责=0.2h  实际推进=0.0h]

━━ 回收候选清单 (3 项) ━━━━━━━━━━━━━━━━
[reference cycle #0] 互相担保，谁都不连 root：
   ⇄ #3 Learn Rust
   ⇄ #4 Rewrite blog in Rust
[unreachable] 从任何活跃目标都不可达：
   · #5 Orphan task
```

> Note: the GC log/heap map/`doctor`/`stats` output above (`report.py`) is not yet
> covered by the v0.4 i18n pass — its labels stay in Chinese regardless of `--lang`.
> Task titles you type yourself (e.g. "Learn Rust" above) are of course whatever
> language you typed them in. See [v0.4](#v04-bilingual-en--zh-i18n) below.

### What a `heap` (alias `jmap`) looks like

```
━━ 堆内存快照 · jmap -heap ━━━━━━━━━━━━━━━━
  -Xmx 占用: 5 / 50 obj  [██░░░░░░░░░░░░░░░░░░] 10.0%   (Metaspace 不计)

  ┌ Eden · 新生代 (5)
  │   ★ #1 Ship payment refactor this quarter
  │     #2 Write reconciliation module
  │   ☠ #3 Learn Rust  (cycle #0)
  │   ☠ #4 Rewrite blog in Rust  (cycle #0)
  │   ☠ #5 Orphan task  (unreachable)
  ├ Survivor · 幸存区 (0)
  │   (空)
  ├ Old · 老年代 (0)
  │   (空)
  ├ Metaspace · 元空间 (1)
  │   ⊛ #6 Be more disciplined
  └──────────────
  图例: ★ root · ☠ 回收候选(僵尸) · ⊛ 元任务
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
- A **Language / 语言** picker at the top of the sidebar — see [v0.4](#v04-bilingual-en--zh-i18n)

The Web UI is optional: without `streamlit` installed, `todogc web` just prints a friendly
install hint and never affects other commands; the browser's auto-refresh doesn't count as
"viewing" (it doesn't inflate the view counter).

## v0.2: Policies, Reference Strength & Cognitive Leak Diagnosis

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

The database auto-migrates from older schemas to v4; a `.bak` is generated alongside
the file before migrating. A real GC run writes to the audit history; `heap` and
`gc --dry-run` are always read-only.

## v0.3: Scheduled Checkup (ntfy push)

`todogc watch` means you don't have to manually run a command to check on things —
per the `[notify]`/`[watch]` config in `todogc.toml` (full fields in
`todogc.toml.example`), it polls, and pushes an [ntfy](https://ntfy.sh) notification
on state transitions:

```toml
[notify]
ntfy_enabled = true
ntfy_topic = "your-topic"        # pick something hard to guess; ntfy_token only needed for protected topics

[watch]
poll_seconds = 1800              # how often to check
digest_seconds = 86400           # send a heartbeat digest periodically even with no new events
remind_days = 7                  # re-remind after this many days if a candidate's state hasn't changed
heap_threshold_pct = 80          # heap usage alert threshold (%)
```

The push design is **event-driven**, not heartbeat noise — modeled on how a real JVM
only logs when a collection actually happens. `todogc watch` only pushes on the
following "transitions" (unchanged candidate count/state stays silent):

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

## v0.4: Bilingual (EN / ZH) i18n

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
language switcher (the `Language / 语言` dropdown at the top of the sidebar), seeded from
`todogc.toml`'s `[i18n] lang`, but switching only affects the current session — it's never
written back to the config file, and doesn't affect anyone else's page. The background
`todogc watch` push language only follows `[i18n] lang` (or `TODOGC_LANG`) — deliberately
decoupled from the Web UI's display language, so multiple people with the Web UI open at
once can't change each other's push language.

> Currently covers CLI output and `--help` text, the Web UI, ntfy push content, and
> config/store-layer error messages. The GC log, heap map, and `doctor`/`stats` output
> (`report.py`) still isn't part of this pass and stays in Chinese regardless of
> `--lang` — that's un-migrated territory, PRs welcome.

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
  cli.py        command line + Stop-the-world ceremony + todogc watch + --lang
  webapp.py     Streamlit Web UI (heap map + add/gc/restore interaction + auto-mounted watcher + language switch, optional)
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
python -m build          # produces dist/todogc-0.4.0-py3-none-any.whl and .tar.gz
pip install dist/todogc-0.4.0-py3-none-any.whl   # install it anywhere
```

`dist/`, `build/`, `*.egg-info/` are all in `.gitignore` — build artifacts aren't
tracked. To include the Web UI: `pip install "todogc[web]"`.

## Status & Roadmap

**Done**: the full MVP (entry / touch tracking / reachability + reference cycles /
collection ceremony / trash restore), JVM-style GC log, `finalize()` eulogies
(including "can only revive once"), generational roasting, Metaspace meta-tasks,
`-Xmx` heap capacity constraint (`add` triggers Allocation Failure when full, never
auto-deletes), `rich` colored output (optional), heap map (`heap`/`jmap` generational
snapshot + -Xmx usage bar + zombies in red), Web UI (`web`: Streamlit heap map +
add/gc/restore interaction, optional), standard packaging (`python -m build` → wheel/sdist).

**v0.2 done**: database migration and backup, consecutive-skip/restore grace period,
strong/soft/weak, gentle/normal/aggressive, show/edit/history/link, doctor, GC history
stats, Web task details and Leak Inspector, input validation and adversarial tests.

**v0.3 done**: `todogc watch` scheduled checkup, ntfy push (event-driven + debounce
remind period + heartbeat digest + edge-triggered heap alerts), advisory
single-instance lock, Web UI auto-mounts a background watcher thread per config.

**v0.4 done**: bilingual EN/ZH, English by default; `--lang`/env var/`todogc.toml`
three-tier precedence; Web UI switches language independently per session; CLI output,
`--help`, Web UI, ntfy push, and config/store-layer errors are all covered
(`report.py`'s GC log/heap map is not yet included).

**Candidates for later (PRs welcome)**: GitHub Issues/PR sync, TODO/FIXME import,
interactive reference graph, bringing `report.py`'s GC log/heap map text into i18n.

> Note: a task's "age" currently only increments **when revived during the collection
> ceremony** (i.e. "how many times you've dodged it"), not automatically every GC round.

## License

MIT
