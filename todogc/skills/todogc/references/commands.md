# Task operations

Append these commands to the fixed Python/database/config prefix in `SKILL.md`.
Replace example IDs with actual CLI results. All titles and notes are arguments,
not shell code. Global flags such as `--db` and `--config` precede the subcommand.

| Intent | Command |
|---|---|
| Capture an idea | `capture "Explore an IBKR trading system"` |
| Read pending ideas | `inbox` |
| Declare an active goal | `add "Finish the IBKR trading system" --root` |
| Add a child or grandchild | `add "Test order submission" --root-id 7` |
| Organize an idea or move a task | `link 8 7` |
| Remove a parent link | `link 8 --off` |
| Make an existing task a goal | `root 7` |
| Find existing work | `search "IBKR" --scope all` |
| Search titles and history notes | `search "feedback" --notes --scope active` |
| Inspect details | `show 7` |
| Read recent events | `history 7 --limit 10` |
| Correct a title | `edit 7 "Finish the IBKR trading system"` |
| Record actual progress | `progress 7 "Order submission verified"` |
| Complete successfully | `done 7 "Validation complete"` |
| Complete a whole branch | `done 7 --with-subtasks` |
| List completed work | `completed` |
| Reopen completed work | `reopen 7` |
| Show the hierarchy | `tree --show-completed --show-snoozed` |
| Read today's plan | `today` |
| Select one task for today | `focus 7` |
| Select a branch for today | `focus 7 --with-subtasks` |
| Reorder a selected item | `focus 7 --position 1` |
| Remove a selection/dismiss a suggestion | `unfocus 7` |
| Pause for a duration | `snooze 7 --for 2h --note "Waiting for feedback"` |
| Pause until a future time | `snooze 7 --until "2030-10-25T09:00+09:00"` |
| Read sleeping tasks | `snoozed` |
| Wake early | `unsnooze 7` |
| Record an unscheduled skip | `defer 7 "Postponed"` |
| Inspect GC candidates | `gc --dry-run` |
| Read collected/discarded work | `graveyard` |
| Restore collected work | `restore 7 --root-id 1` |
| Discard an Inbox idea | `discard 7 "Not pursuing it"` |
| Generate a completion review | `review --week this` |
| Export a share card | `review --week last --export "/absolute/path/weekly.svg"` |
| Read the notification service state | `watch status --json` |

## Rules that affect task decisions

- `search` defaults to all states and is read-only. Default `ls` excludes Inbox,
  sleeping, Done, and Collected work and records views for displayed results.
  Prefer `search`, `show`, `tree`, and `today` for inspection without view events.
- `--root-id` is the immediate parent, including an ordinary subtask. Parent cycles
  are rejected. An ordinary unlinked task is allowed, but may become a GC candidate.
  Inbox ideas are protected while awaiting organization and cannot be parents yet.
- The usual capacity limit is 50 unfinished non-meta tasks. Inbox and Snooze count
  toward it. Handle a capacity refusal without silently adding `--force`.
- `done` refuses a parent with unfinished descendants unless the user chooses to
  complete the branch. Completing a child does not finish its parent. `reopen`
  affects one task, whereas `restore` recovers collected/discarded work.
- Today is an ordered, persistent plan for the machine's local calendar day.
  Yesterday's unfinished items and today's wake-ups are suggestions, not automatic
  selections. Focused goals do not automatically select their children.
- Snooze durations accept `m`, `h`, `d`, and `w`; absolute times default to local
  time without an offset. Deadline visibility works without Watch. Push reminders
  require ntfy configuration and an explicitly started watcher.
- `review` summarizes completions and decluttering; `stats --since 30d` summarizes
  GC audits. SVG export has no extra dependencies; PNG requires the export extra.
