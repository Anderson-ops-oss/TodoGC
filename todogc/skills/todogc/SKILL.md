---
name: todogc
description: Manage persistent personal tasks and daily plans in TodoGC when the user asks to capture, organize, update, or review their work.
---

# TodoGC

Operate the user's task database through the installed TodoGC CLI. This skill is
for managing tasks, rather than developing TodoGC or adding TODO comments to code.

## Use the installed runtime

Read `runtime.json` beside this file before operating on tasks. The installer
records absolute `python`, `database`, and `config` paths. Construct commands as:

```text
"<python>" -m todogc.cli --db "<database>" --config "<config>" <command> <arguments>
```

Quote paths and user text for the current shell. Use the recorded Python executable,
including its venv/pipx path, instead of guessing which `todogc` is on PATH. Keep the
same database/config when the conversation changes projects. If the database's
parent directory does not exist on first use, create that directory before the
first task command; do not create an empty database yourself or edit SQLite directly.
The configuration file may be absent: TodoGC then uses its defaults.

If `runtime.json` is missing, install this bundled skill with
`todogc skill install --target codex` or `--target claude-code`. If the recorded
Python was removed, locate the user's current TodoGC installation and reinstall
the skill. Preserve the chosen database; do not silently start a replacement list.

Read [references/commands.md](references/commands.md) for task operations and use
the installed CLI's `--help` for options not covered there.

## Choose the requested operation

- Capture tentative ideas in Inbox. Use ordinary tasks and parent links for work
  the user has committed to; declare a Root only when it is an active goal.
- When the user refers to existing work, search or inspect it and reuse its ID.
  Disambiguate genuinely ambiguous matches before changing them.
- Record actual work with `progress`, and explicit completion with `done`. Planning,
  discussion, passing time, or ending an AI response does not establish progress
  or completion. Honor an existing instruction to update a task after verified work.
- Correct titles with `edit`; preserve the task ID and history. An EDIT event also
  counts toward TodoGC's progress metrics, so describe it as an edit accurately.
- `focus` selects only one task. Include descendants with `--with-subtasks` when
  the user requests a branch. Today planning does not protect tasks from GC.
- A scheduled pause needs a future duration/time. Use `snooze` for it, and `defer`
  only when the user means an unscheduled skip. Preserve supplied timezone offsets.
- Use `gc --dry-run` to inspect candidates. Collect/discard work, exceed capacity
  with `--force`, or enable/start push notifications only within the user's request.
  The CLI performs lifecycle validation; report a refusal instead of bypassing it.

Routine changes explicitly requested by the user can proceed without a second
confirmation. Report the affected IDs, titles, and actual result from CLI output.
Preserve the user's language in titles and notes; respond in their preferred language.
