# en.py — the English message catalog; the source of truth for which keys exist across the project.

from __future__ import annotations

MESSAGES: dict[str, str] = {
    "store.error.parent_cycle": "Cannot move a task under one of its descendants. Choose a parent outside this subtree.",
    "webapp.tree.title": "Tasks",
    "webapp.tree.show_completed": "Show completed",
    "webapp.tree.group.goals": "Goals",
    "webapp.tree.group.unlinked": "Unlinked",
    "webapp.tree.group.meta": "Meta-tasks",
    "webapp.tree.expand": "Expand subtasks",
    "webapp.tree.collapse": "Collapse subtasks",
    "webapp.tree.legacy_cycle": "Some parent links form a cycle. The affected tasks are shown here so you can move them to a valid parent.",
    "webapp.tree.empty": "No active tasks. Add a goal or task in the sidebar, or show completed tasks.",
    "webapp.tree.path": "Task path",
    "webapp.tree.generation_label": "Generation: {name}",
    "webapp.tree.generation.eden": "Eden",
    "webapp.tree.generation.survivor": "Survivor",
    "webapp.tree.generation.old": "Old",
    "webapp.tree.generation.meta": "Metaspace",
    "webapp.tree.move_to": "Move to",
    "webapp.tree.move_button": "Move task",
    "webapp.tree.no_parent": "No parent (unlinked)",
    "webapp.tree.add_parent": "Parent / goal (optional)",
    "webapp.tree.add_under": "Add a subtask under #{id} {title}",
    "webapp.tree.subtask_title": "Subtask title",
    "webapp.tree.add_subtask": "Add subtask",
    "webapp.tree.reopen_parent": "Parent on reopen",
    "webapp.tree.keep_parent": "Keep original parent",
    "store.error.task_already_done": "Task #{id} is completed; reopen it first.",
    "store.error.task_not_done": "Task #{id} is not completed; only completed tasks can be reopened.",
    "store.error.unfinished_subtasks": "Task #{id} has unfinished subtasks: {tasks}. Complete or reassign them first, or explicitly include them with --with-subtasks (Include unfinished subtasks in the Web UI).",
    "store.error.reopen_parent_inactive": "Parent #{id} is inactive. Reopen it first, or choose an active parent with --root-id.",
    "store.error.reopen_heap_full": "Active heap is full (cap {max}). Complete or collect a task first, increase the cap, or use reopen --force.",
    "cli.done.success": "Completed {ids}. Removed from the active heap; history preserved.",
    "cli.reopen.success": "Reopened #{id}. History preserved; a grace period applies.",
    "cli.completed.empty": "No completed tasks yet.",
    "cli.help.cmd_done": "mark a task as completed",
    "cli.help.done_subtasks": "also complete every unfinished descendant (explicit opt-in)",
    "cli.help.cmd_reopen": "return a completed task to the active heap",
    "cli.help.cmd_completed": "list completed tasks and completion times",
    "webapp.sidebar.language_label": "Language",
    "webapp.language.en": "English",
    "webapp.language.zh": "Chinese",
    "webapp.done.button": "Mark as done",
    "webapp.done.unfinished": "Unfinished subtasks — complete or reassign these first, or explicitly include them below:",
    "webapp.done.with_subtasks": "Include unfinished subtasks when marking done",
    "webapp.completed.title": "Completed",
    "webapp.completed.timestamp": "Completed at: {time}",
    "webapp.completed.parent": "Parent / goal ID on reopen (0 = keep existing)",
    "webapp.completed.reopen": "Reopen",
    # collector.py
    "collector.error.invalid_mode": "GC mode must be gentle / normal / aggressive",
    "collector.error.invalid_threshold": "GCPolicy thresholds must be greater than 0",
    "collector.detail.cycle": "Trapped in reference cycle #{cycle_id}: mutually vouching, none reaches a root",
    "collector.detail.unreachable": "Unreachable from any live target",
    "collector.detail.stale": (
        "Reachable but dead: {refcount} touches in the last {days} days, "
        "skipped {skips} times in a row"
    ),
    # notify.py
    "notify.error.empty_topic": "ntfy topic must not be empty",
    "notify.error.invalid_priority": "priority must be between 1 and 5",
    "notify.error.http": "ntfy returned {code}: {reason}",
    "notify.error.network": "ntfy push failed: {error}",
    # watcher.py
    "watcher.error.ntfy_disabled": (
        "Watcher requires notify.ntfy_enabled to be turned on and "
        "notify.ntfy_topic to be configured first"
    ),
    "watcher.lock.already_running": (
        "todogc watch: another watcher is already running, this process will not push duplicate notifications"
    ),
    "watcher.push.failed": 'todogc watch: push "{title}" failed: {error}',
    "watcher.push.event.title.one": "TodoGC · New candidate",
    "watcher.push.event.title.other": "TodoGC · {count} new candidates",
    "watcher.push.event.line": "{icon} #{task_id} {title} — {detail}",
    "watcher.push.heap_usage": "Heap usage {size}/{max} ({pct}%)",
    "watcher.push.heap_alarm.title": "TodoGC · Heap usage alert",
    "watcher.push.heap_alarm.body": "🚨 -Xmx approaching its limit: {size}/{max} ({pct}%)",
    "watcher.push.digest.title": "TodoGC · Scheduled checkup",
    "watcher.push.digest.body": "{count} candidates · heap usage {size}/{max} ({pct}%)",
    # cli.py: generic errors
    "cli.error": "✗ {error}",
    "cli.error.not_found": "✗ #{id} not found",
    "cli.error.config": "✗ config error: {error}",
    # cli.py: run_ceremony
    "cli.ceremony.finalize_spent": "finalize() already called — revived before, nothing to say this time",
    "cli.ceremony.silent_collect": "(silent collection, no eulogy)",
    # cli.py: add
    "cli.add.heap_full": "[GC (Allocation Failure)] active heap {size}/{max} full — reclaim before allocating.",
    "cli.add.hint_gc_or_force": (
        "→ Run `todogc gc` to clear zombies and free space, or `add --force` to over-allocate anyway."
    ),
    "cli.add.full_gc_no_candidates": (
        "[Full GC (Ergonomics)] and no reclaimable zombies — -Xmx is maxed out, GC is powerless."
    ),
    "cli.add.hint_cut_scope": "→ Cut scope, don't add memory (force it anyway: add --force).",
    "cli.add.allocated": "Allocated #{id}: {title}{tag}",
    # cli.py: root
    "cli.root.off": "#{id} unrooted",
    "cli.root.on": "#{id} set as ★root",
    # cli.py: dep
    "cli.dep.removed": "Removed reason edge #{task_id} → #{ref_id}",
    "cli.dep.added": "#{task_id}'s reason → #{ref_id}",
    # cli.py: ls
    "cli.ls.empty_heap": "(empty heap)",
    # cli.py: progress
    "cli.progress.done": "#{id} +1 refcount. That's what counts.",
    # cli.py: snooze
    "cli.snooze.done": "#{id} pushed back again. skip_count +1, it's keeping score.",
    # cli.py: gc
    "cli.gc.stw_banner": "\n—— Stop-the-world: world paused, handling one by one ——",
    "cli.gc.prompt_choice": "    [r]revive  [c]collect  [s]skip > ",
    "cli.gc.prompt_eulogy": "    finalize() eulogy (enter = silent collection) > ",
    "cli.gc.no_root_to_revive": "    ⚠ No root to revive to — add --root first; keeping as-is for now.",
    "cli.gc.ceremony_done": "\n[GC] Ceremony done: revived {revived} · collected {collected} · skipped {skipped}",
    # cli.py: web
    "cli.web.not_installed": (
        'streamlit not installed — the Web UI is optional.\n  Install: pip install -e ".[web]"\n'
        "  Then run: todogc web"
    ),
    "cli.web.starting": "Starting Web UI (db {db} · -Xmx {heap_max}) — Ctrl-C to stop",
    # cli.py: watch
    "cli.watch.not_configured": (
        "Scheduled push not enabled — configure this in todogc.toml first:\n"
        "  [notify]\n"
        "  ntfy_enabled = true\n"
        '  ntfy_topic = "your-topic"\n'
        "then run todogc watch"
    ),
    "cli.watch.starting": "todogc watch started (db {db} · checking every {poll_seconds}s) — Ctrl-C to stop",
    "cli.watch.stopped": "todogc watch stopped",
    # cli.py: graveyard
    "cli.graveyard.empty": "The graveyard is empty. You haven't been honest with yourself yet.",
    "cli.graveyard.entry": "† {title}   lived {days}d · progressed {refcount} · viewed {views}",
    "cli.graveyard.epitaph": '  "{note}"',
    # cli.py: restore
    "cli.restore.not_collected": "✗ #{id} has not been collected",
    "cli.restore.invalid_root": "Restore target #{root_id} does not exist or was already collected",
    "cli.restore.self_link": "A task cannot be restored under itself",
    "cli.restore.done": "#{id} restored. Slip-ups happen, this one doesn't count as self-deception.",
    # cli.py: edit
    "cli.edit.needs_field": "✗ edit needs a new title or --ref",
    "cli.edit.done": "#{id} updated: {title} [{ref}]",
    # cli.py: link
    "cli.link.unlinked": "#{id} unlinked",
    "cli.link.needs_root_id": "link needs root_id, or use --off",
    "cli.link.linked": "#{id} linked to #{root_id}",
    # cli.py: argparse type parsers
    "cli.parse.time_range_invalid": "time range should be like 30d or a positive integer",
    "cli.parse.time_range_positive": "time range must be greater than 0",
    "cli.parse.must_be_positive_int": "must be a positive integer",
    "cli.parse.must_be_positive": "must be greater than 0",
    # cli.py: argparse help text
    "cli.help.description": "Coldly garbage-collect the tasks you'll never do.",
    "cli.help.db": "database path",
    "cli.help.config": "config file path (default: todogc.toml)",
    "cli.help.heap_max": "active heap limit (-Xmx); triggers Allocation Failure when full",
    "cli.help.max_skips": "consecutive-skip threshold",
    "cli.help.lang": "output language (en/zh)",
    "cli.help.cmd_add": "add a task",
    "cli.help.add_root_id": "id of the parent/target to link to",
    "cli.help.add_root": "mark this task itself as an active root",
    "cli.help.add_meta": "mark as a meta-task (Metaspace)",
    "cli.help.add_ref": "reference strength",
    "cli.help.add_force": "force-allocate when the heap is full (skip Allocation Failure)",
    "cli.help.cmd_root": "mark/unmark an active root",
    "cli.help.root_off": "unmark as root",
    "cli.help.cmd_dep": "record/remove a reason edge (a task's reason is its ref)",
    "cli.help.cmd_ls": "list tasks",
    "cli.help.cmd_progress": "record a real bit of progress",
    "cli.help.cmd_snooze": "snooze a task",
    "cli.help.cmd_gc": "trigger garbage collection + the collection ceremony",
    "cli.help.gc_full": "render as a Full GC",
    "cli.help.gc_dry_run": "report only, skip the ceremony",
    "cli.help.cmd_heap": "print the generational heap map (zombies in red)",
    "cli.help.cmd_web": "start the Streamlit Web UI (heap map + interaction, needs the [web] extra)",
    "cli.help.web_port": "Web UI port (default: streamlit's 8501)",
    "cli.help.cmd_watch": "scheduled checkup; pushes to ntfy on state transitions/heap alerts (needs [notify])",
    "cli.help.cmd_graveyard": "epitaph wall",
    "cli.help.cmd_restore": "restore from the trash",
    "cli.help.restore_root_id": "re-link to a parent/target on restore",
    "cli.help.cmd_show": "show task details",
    "cli.help.cmd_history": "show a task's event timeline",
    "cli.help.cmd_edit": "edit title or reference strength",
    "cli.help.cmd_link": "link or unlink a parent/target",
    "cli.help.cmd_doctor": "diagnose cognitive memory leaks",
    "cli.help.cmd_stats": "view GC history stats",
    "cli.help.stats_since": "e.g. 30d",
    # webapp.py: shared fields
    "webapp.field.title": "Title",
    "webapp.field.ref_strength": "Reference strength",
    # webapp.py: page/sidebar chrome
    "webapp.page.title": "TodoGC · Heap Memory",
    "webapp.sidebar.db_caption": "DB: `{db}`",
    "webapp.sidebar.watcher_running": "🔔 Background checkup running · every {poll_seconds}s · ntfy `{topic}`",
    "webapp.sidebar.watcher_disabled": "🔕 [notify] not configured, scheduled checkup disabled",
    "webapp.sidebar.heap_max_label": "-Xmx (active heap limit)",
    "webapp.sidebar.full_checkbox": "Render log as Full GC",
    "webapp.sidebar.mode_label": "GC policy",
    "webapp.main.heap_title": "Heap Memory Snapshot · jmap -heap",
    "webapp.main.gc_log_subheader": "GC Log",
    "webapp.main.ceremony_subheader": "Stop-the-world Collection Ceremony",
    "webapp.main.task_manager_subheader": "Task Details · history/edit/progress",
    "webapp.main.stats_subheader": "GC History · stats",
    "webapp.main.graveyard_subheader": "Graveyard · graveyard",
    # webapp.py: heap view
    "webapp.heap.progress": "-Xmx usage {used}/{max} obj · {pct}%　(Metaspace excluded)",
    "webapp.heap.legend": "Legend: ★ root · ☠ reclaim candidate (zombie) · ⊛ meta-task",
    "webapp.heap.empty_section": ":gray[(empty)]",
    # webapp.py: report
    "webapp.report.candidates_expander": "Candidate list as text ({count} items)",
    # webapp.py: ceremony
    "webapp.ceremony.no_candidates": "No reclaim candidates. You didn't lie to yourself this time — rare.",
    "webapp.ceremony.choice.skip": "skip",
    "webapp.ceremony.choice.revive": "revive",
    "webapp.ceremony.choice.collect": "collect",
    "webapp.ceremony.decide_label": "Decision",
    "webapp.ceremony.eulogy_label": "Eulogy",
    "webapp.ceremony.eulogy_placeholder": "finalize() eulogy (left when collecting)",
    "webapp.ceremony.execute_button": "▶ Execute Stop-the-world ceremony",
    "webapp.ceremony.done": "Ceremony done: revived {revived} · collected {collected} · skipped {skipped}",
    # webapp.py: add
    "webapp.add.subheader": "➕ Allocate task (add)",
    "webapp.add.root_checkbox": "Mark as an active root (★ target)",
    "webapp.add.root_id_label": "Link to root-id (0 = none)",
    "webapp.add.meta_checkbox": "Meta-task (Metaspace, excluded from -Xmx)",
    "webapp.add.force_checkbox": "--force over-allocate",
    "webapp.add.submit_button": "Allocate",
    "webapp.add.heap_full": "[GC · Allocation Failure] active heap {size}/{max} full — reclaim before allocating.",
    "webapp.add.hint_gc_or_force": (
        "→ Run the Stop-the-world ceremony above to clear zombies, or check --force to over-allocate."
    ),
    "webapp.add.full_gc_no_candidates": (
        "[Full GC · Ergonomics] and nothing reclaimable — -Xmx is maxed out, cut scope, not memory."
    ),
    "webapp.add.allocated": "Allocated #{id}: {title}",
    # webapp.py: graveyard
    "webapp.graveyard.entry": "† **{title}**   lived {days}d · progressed {refcount} · viewed {views}",
    "webapp.graveyard.epitaph": '"{note}"',
    "webapp.graveyard.restore_button": "Restore",
    # webapp.py: task manager
    "webapp.task_manager.empty": "No active tasks to manage.",
    "webapp.task_manager.select_label": "Select task",
    "webapp.task_manager.note_label": "Event note",
    "webapp.task_manager.save_button": "Save edit",
    "webapp.task_manager.progress_button": "+1 real progress",
    "webapp.task_manager.defer_button": "Snooze",
    "webapp.task_manager.parent_label": "Link to parent/target (0 = unlink)",
    "webapp.task_manager.link_button": "Update link",
    "webapp.task_manager.dep_id_label": "Reason/prerequisite task ID",
    "webapp.task_manager.dep_add_button": "Add reason edge",
    "webapp.task_manager.dep_remove_button": "Remove reason edge",
    "webapp.task_manager.deps_prefix": "Current reason edges: {deps}",
    "webapp.task_manager.deps_none": "none",
    "webapp.task_manager.timeline_expander": "Event timeline ({count})",
    # config.py
    "config.error.read_failed": "Unable to read config {path}: {error}",
    "config.error.unsupported_value": "Unsupported config value: {value}",
    "config.error.empty_section": "line {lineno}: empty section",
    "config.error.invalid_format": "line {lineno}: invalid config format",
    "config.error.empty_key": "line {lineno}: empty config key",
    "config.error.must_be_positive_int": "{name} must be a positive integer",
    "config.error.must_be_positive": "{name} must be greater than 0",
    "config.error.must_be_bool": "{name} must be a boolean (true/false)",
    "config.error.must_be_percent": "{name} must be between 1 and 100",
    "config.error.gc_heap_sections": "[gc] and [heap] must be config sections",
    "config.error.i18n_section": "[i18n] must be a config section",
    "config.error.notify_watch_sections": "[notify] and [watch] must be config sections",
    "config.error.invalid_gc_mode": "gc.mode must be gentle / normal / aggressive",
    "config.error.invalid_lang": "i18n.lang must be one of {langs}",
    "config.error.ntfy_topic_required": "notify.ntfy_topic is required when notify.ntfy_enabled is true",
    # heap.py
    "heap.error.not_persisted": "Task has not been persisted yet, no id available",
    # store.py
    "store.error.insert_no_rowid": "SQLite INSERT did not return a lastrowid",
    "store.error.empty_title": "Task title cannot be empty",
    "store.error.self_link": "A task cannot be linked to itself",
    "store.error.update_requires_persisted": "update_task requires a persisted Task (non-null id)",
    "store.error.task_not_found": "Task #{id} does not exist",
    "store.error.task_already_collected": "Task #{id} already collected, restore it first",
    "store.error.self_dep": "A task cannot use itself as its own reason (no self-loops)",
    # report.py: generation_taunt
    "report.taunt.meta": "Permanent meta-task. This isn't a todo, it's your personality.",
    "report.taunt.tenured": "Tenured. Congratulations, you've turned self-deception into a long-term strategy.",
    "report.taunt.veteran": "Veteran-in-waiting — you've skipped it {age} times.",
    "report.taunt.survivor": "Young gen survivor, still young, still salvageable.",
    "report.taunt.calm": "All quiet.",
    # report.py: format_gc_log
    "report.gc_log.cap_suffix": " (cap {heap_max})",
    "report.gc_log.header": "[uptime {uptime}][info][gc] GC({gc_id}) Pause {kind} ({cause})",
    "report.gc_log.active_heap": "  active heap    : {total} -> {after} tasks{cap}",
    "report.gc_log.reclaimed": "  reclaimed      : {reclaim} zombies   →  cognitive memory freed {pct}%",
    "report.gc_log.cycles": "  reference cycle: {count} found  {names}",
    "report.gc_log.pause": (
        "  pause time     : {duration}   "
        "(the real time you spent agonizing over each one at the ceremony)"
    ),
    "report.gc_log.times": "  [Times: anxiety={anxiety}h  self-blame={blame}h  actual progress=0.0h]",
    # report.py: format_candidates
    "report.candidates.header": "━━ Reclaim candidates ({count} items) ━━━━━━━━━━━━━━━━",
    "report.candidates.cycle_line": "[reference cycle #{id}] mutually vouching for each other, none reaches a root:",
    "report.candidates.unreachable_line": "[unreachable] unreachable from any active target:",
    "report.candidates.stale_line": "[stale] reachable, but its liveness signal is dead:",
    "report.candidates.recent_progress": "progressed {count} times recently",
    "report.candidates.consecutive_skips": "skipped {count} times in a row",
    "report.candidates.viewed": "viewed {count} times",
    "report.candidates.empty": "   (No candidates. You actually didn't lie to yourself this time — rare.)",
    # report.py: format_heap_map
    "report.heap_map.title": "━━ Heap Memory Snapshot · jmap -heap ━━━━━━━━━━━━━━━━",
    "report.heap_map.usage_with_max": "  -Xmx usage: {used} / {max} obj  {bar} {pct}%   (Metaspace excluded)",
    "report.heap_map.usage_no_max": "  heap objects: {used} (no -Xmx set)   (Metaspace excluded)",
    "report.heap_map.empty_row": "  │   (empty)",
    "report.heap_map.legend": "  Legend: ★ root · ☠ reclaim candidate (zombie) · ⊛ meta-task",
    # report.py: candidate_note
    "report.candidate_note.stale_detail": "stale · progressed {progress} skipped {skips}",
    # report.py: format_doctor
    "report.doctor.viewed_no_progress": "viewed {views} times but never progressed",
    "report.doctor.advice.rename_actionable": "Rename it to something you can execute within 30 minutes",
    "report.doctor.consecutive_skips": "skipped {skips} times in a row",
    "report.doctor.advice.progress_or_admit": "Make real progress right now, or admit it should be reclaimed",
    "report.doctor.old_zero_progress": "Old Generation age={age} with zero progress",
    "report.doctor.advice.downgrade_weak": "Downgrade to a weak reference",
    "report.doctor.churn": "{churn} collect/revive/restore events — looks like GC thrashing",
    "report.doctor.advice.relink_or_collect": "Re-link it to a clear root, or reclaim it for good",
    "report.doctor.in_cycle": "sits in an unreachable reference cycle #{cycle_id}",
    "report.doctor.advice.break_cycle": "Break the circular reason-dependency",
    "report.doctor.unreachable_from_roots": "unreachable from every active root",
    "report.doctor.advice.link_or_collect": "Link it to a real goal, or reclaim it right now",
    "report.doctor.stale_by_policy": "reachable, but the current GC policy already flags it as stale",
    "report.doctor.advice.log_progress_or_adjust": "Log real progress, adjust its reference strength, or reclaim it",
    "report.doctor.is_root": "this object is itself a GC root",
    "report.doctor.ok": "[OK] No obvious cognitive memory leaks detected.",
    # report.py: format_stats
    "report.stats.empty": "No GC history yet. Run a non-dry-run `todogc gc` first.",
    "report.stats.title": "━━ GC History Stats ━━━━━━━━━━━━━━━━━━━",
}
