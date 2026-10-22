"""Background lifecycle and diagnostics; workers reuse the foreground Watcher."""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from todogc.config import AppConfig
from todogc.i18n import t
from todogc.notify import NotifyError, NtfyChannel
from todogc.scheduling import latest_weekly_due, local_time, next_weekly_due
from todogc.store import Store
from todogc.watcher import Watcher, public_watch_settings, record_notification, watch_error

_children: dict[str, subprocess.Popen[bytes]] = {}


def _reap_children() -> None:
    for token, process in list(_children.items()):
        if process.poll() is not None:
            _children.pop(token, None)


class WatchServiceError(RuntimeError):
    pass


def database_path(value: str) -> Path:
    if value == ":memory:":
        raise WatchServiceError(t("service.error.memory"))
    return Path(value).expanduser().resolve()


def log_path(value: str) -> Path:
    db = database_path(value)
    return db.with_name(db.name + ".watch") / "watch.log"


def _read_meta(value: str) -> dict[str, str]:
    """Status/log inspection never creates a database or migrates its schema."""
    db = database_path(value)
    if not db.exists():
        return {}
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'watch_meta' AND type = 'table'").fetchone() is None:
            return {}
        return dict(conn.execute("SELECT key, value FROM watch_meta"))


def _object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _time(raw: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw or "")
        return value if value.tzinfo is not None else None
    except ValueError:
        return None


def _pid(raw: str | None) -> int | None:
    try:
        value = int(raw or "0")
        return value if value > 0 else None
    except ValueError:
        return None


def _dead(pid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)  # existence probe only; stop never sends process signals
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def service_status(db_path: str, config: AppConfig, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    meta = _read_meta(db_path)
    known = {field.name for field in fields(AppConfig)} - {"ntfy_token", "ntfy_server"}
    saved = {key: value for key, value in _object(meta.get("watch_owner_settings")).items() if key in known}
    effective = AppConfig(**saved) if saved else config
    try:
        stale_after = timedelta(seconds=effective.watch_lock_stale_seconds)
        due = latest_weekly_due(now, weekday=effective.watch_weekly_review_day, clock=effective.watch_weekly_review_time,
                                timezone_name=effective.watch_weekly_review_timezone)
        upcoming = next_weekly_due(now, weekday=effective.watch_weekly_review_day, clock=effective.watch_weekly_review_time,
                                  timezone_name=effective.watch_weekly_review_timezone)
    except (ValueError, TypeError, OverflowError):
        effective = config
        stale_after = timedelta(seconds=config.watch_lock_stale_seconds)
        due = latest_weekly_due(now, weekday=config.watch_weekly_review_day, clock=config.watch_weekly_review_time,
                                timezone_name=config.watch_weekly_review_timezone)
        upcoming = next_weekly_due(now, weekday=config.watch_weekly_review_day, clock=config.watch_weekly_review_time,
                                  timezone_name=config.watch_weekly_review_timezone)
    pid = _pid(meta.get("watch_lock_pid"))
    heartbeat = _time(meta.get("watch_lock_heartbeat_at"))
    token = meta.get("watch_lock_token")
    if pid is None:
        state = "failed" if meta.get("watch_last_exit") == "failed" else "stopped"
    elif heartbeat is None or now - heartbeat >= stale_after or _dead(pid):
        state = "unresponsive"
    elif token and meta.get("watch_stop_requested_token") == token:
        state = "stopping"
    else:
        state = "running"
    key = (f"weekly_review:{effective.watch_weekly_review_timezone}:{effective.watch_weekly_review_day}:"
           f"{effective.watch_weekly_review_time}:{due.isoformat()}:sent")
    sent = [_time(value) for key, value in meta.items() if key.startswith("weekly_review:") and key.endswith(":sent")]
    last_weekly = max((value for value in sent if value is not None), default=None)
    last_check = _time(meta.get("watch_last_check_at"))
    return {
        "state": state, "database": str(database_path(db_path)), "pid": pid,
        "mode": meta.get("watch_owner_mode") if pid is not None else None,
        "started_at": meta.get("watch_owner_started_at"), "heartbeat_at": meta.get("watch_lock_heartbeat_at"),
        "stopped_at": meta.get("watch_last_stopped_at"), "last_check_at": meta.get("watch_last_check_at"),
        "last_check_finished_at": meta.get("watch_last_check_finished_at"),
        "last_check_error": meta.get("watch_last_check_error") or None,
        "next_check_at": (last_check + timedelta(seconds=effective.watch_poll_seconds)).isoformat()
                         if last_check is not None and state == "running" else None,
        "next_weekly_at": upcoming.isoformat() if effective.watch_weekly_review_enabled else None,
        "pending_weekly_at": due.isoformat() if effective.watch_weekly_review_enabled and key not in meta else None,
        "last_weekly_sent_at": last_weekly.isoformat() if last_weekly else None,
        "last_notification": _object(meta.get("watch_last_notification")) or None,
        "last_notification_success_at": meta.get("watch_last_notification_success_at"),
        "last_notification_failure_at": meta.get("watch_last_notification_failure_at"),
        "last_notification_error": meta.get("watch_last_notification_error") or None,
        "settings": public_watch_settings(effective),
        "settings_source": "running" if pid is not None and saved else "last_run" if saved else "current",
        "log_path": str(log_path(db_path)),
    }


def format_service_status(status: dict[str, Any]) -> str:
    def stamp(value: str | None) -> str:
        parsed = _time(value)
        return local_time(parsed) if parsed is not None else t("service.never")

    settings = status["settings"]
    lines = [t("service.status.state", state=t(f"service.state.{status['state']}"),
               mode=status["mode"] or "-", pid=status["pid"] or "-"),
             t("service.status.db", path=status["database"]),
             t("service.status.settings", source=t(f"service.source.{status['settings_source']}")),
             t("service.status.interval", seconds=settings["watch_poll_seconds"])]
    for key in ("started_at", "heartbeat_at", "last_check_at", "last_check_finished_at", "next_check_at"):
        lines.append(t(f"service.status.{key}", time=stamp(status[key])))
    lines.append(t("service.status.weekly", day=t(f"service.weekday.{settings['watch_weekly_review_day']}"),
                   clock=settings["watch_weekly_review_time"], zone=settings["watch_weekly_review_timezone"])
                 if settings["watch_weekly_review_enabled"] else t("service.weekly.disabled"))
    lines.append(t("service.status.topic", topic=settings["ntfy_topic"] if settings["ntfy_enabled"] else t("service.disabled")))
    for key in ("next_weekly_at", "pending_weekly_at", "last_weekly_sent_at", "last_notification_success_at", "last_notification_failure_at"):
        lines.append(t(f"service.status.{key}", time=stamp(status[key])))
    notification = status["last_notification"]
    if notification:
        lines.append(t("service.status.notification", kind=notification.get("kind", "-"),
                       result=t("service.sent" if notification.get("ok") else "service.failed")))
    for key in ("last_notification_error", "last_check_error"):
        if status[key]:
            lines.append(t(f"service.status.{key}", error=status[key]))
    lines.append(t("service.status.logs", path=status["log_path"]))
    return "\n".join(lines)


def _prepare_log(db_path: str) -> Path:
    path = log_path(db_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    path.chmod(0o600)
    return path


def configure_watch_logging(db_path: str) -> None:
    path = _prepare_log(db_path)
    logger = logging.getLogger("todogc")
    for existing in list(logger.handlers):
        if getattr(existing, "_todogc_watch", False):
            logger.removeHandler(existing)
            existing.close()
    handler = RotatingFileHandler(path, maxBytes=1_048_576, backupCount=3, encoding="utf-8")
    handler._todogc_watch = True  # pyright: ignore[reportAttributeAccessIssue]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def read_watch_logs(db_path: str, *, lines: int = 50) -> str:
    if lines < 1:
        raise ValueError(t("service.error.lines"))
    path = log_path(db_path)
    tail: deque[str] = deque(maxlen=lines)
    for source in [path.with_name(path.name + f".{i}") for i in (3, 2, 1)] + [path]:
        try:
            with source.open(encoding="utf-8", errors="replace") as stream:
                tail.extend(stream)
        except FileNotFoundError:
            continue
    return "".join(tail).rstrip()


def _require_notifications(config: AppConfig) -> None:
    if not config.ntfy_enabled or not config.ntfy_topic:
        raise WatchServiceError(t("cli.watch.not_configured"))


def start_background(db_path: str, config: AppConfig, *, timeout: float = 10) -> dict[str, Any]:
    _reap_children()
    _require_notifications(config)
    db = str(database_path(db_path))
    status = service_status(db, config)
    if status["state"] in {"running", "stopping"}:
        return status
    # A crashed owner needn't hold up a restart until its heartbeat expires.
    if status["pid"] is not None and _dead(status["pid"]):
        meta = _read_meta(db)
        with Store(db) as store:
            store.release_watch_lock(status["pid"], token=meta.get("watch_lock_token", ""))
    path = _prepare_log(db)
    token = uuid.uuid4().hex
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    with path.open("ab", buffering=0) as output:
        process = subprocess.Popen([sys.executable, "-m", "todogc.watch_service"],
                                   stdin=subprocess.PIPE, stdout=output, stderr=output,
                                   start_new_session=os.name == "posix", creationflags=creation_flags)
    _children[token] = process
    try:
        assert process.stdin is not None
        # Credentials travel over a private pipe, never argv or a persistent config file.
        process.stdin.write(json.dumps({"database": db, "token": token, "config": asdict(config)}).encode())
        process.stdin.close()
    except (OSError, BrokenPipeError):
        process.wait(timeout=timeout)
        _children.pop(token, None)
        raise WatchServiceError(t("service.error.start", path=path)) from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service_status(db, config)
        if status["state"] in {"running", "stopping"}:
            # Atomic acquisition also covers simultaneous foreground/Web/background starts.
            if process.poll() is not None:
                _children.pop(token, None)
            return status
        if process.poll() is not None:
            _children.pop(token, None)
            raise WatchServiceError(t("service.error.start", path=path))
        time.sleep(0.05)
    with Store(db) as store:
        store.request_watch_stop(token, allow_unowned=True)
    raise WatchServiceError(t("service.error.start_timeout", path=path))


def stop_watch(db_path: str, config: AppConfig, *, timeout: float = 15) -> dict[str, Any]:
    _reap_children()
    db = str(database_path(db_path))
    meta = _read_meta(db)
    if _pid(meta.get("watch_lock_pid")) is None:
        return service_status(db, config)
    token = meta.get("watch_lock_token")
    if not token:
        raise WatchServiceError(t("service.error.legacy"))
    pid = _pid(meta.get("watch_lock_pid"))
    if pid is not None and _dead(pid):
        with Store(db) as store:
            store.release_watch_lock(pid, token=token)
        return service_status(db, config)
    with Store(db) as store:
        if not store.request_watch_stop(token):
            return service_status(db, config)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_meta(db).get("watch_lock_token") != token:
            child = _children.pop(token, None)
            if child is not None:
                child.wait(timeout=2)
            return service_status(db, config)
        time.sleep(0.05)
    return service_status(db, config)


def send_test(db_path: str, config: AppConfig) -> None:
    _require_notifications(config)
    channel = NtfyChannel(topic=config.ntfy_topic, server=config.ntfy_server,
                          token=config.ntfy_token, lang=config.lang)
    with Store(str(database_path(db_path))) as store:
        try:
            channel.send(t("service.test.title", lang=config.lang), t("service.test.body", lang=config.lang),
                         priority=3, tags=["white_check_mark"])
        except NotifyError as exc:
            record_notification(store, config, "test", error=exc)
            _append_test_log(db_path, "failed", watch_error(exc, config))
            raise WatchServiceError(watch_error(exc, config)) from exc
        record_notification(store, config, "test")
        _append_test_log(db_path, "sent")


def _append_test_log(db_path: str, result: str, error: str = "") -> None:
    path = _prepare_log(db_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now(timezone.utc).isoformat()} Test notification {result} {error}\n")


def _worker() -> int:
    config = AppConfig()
    try:
        payload = json.load(sys.stdin)
        sys.stdin.close()
        config = AppConfig(**payload["config"])
        db = str(database_path(payload["database"]))
        configure_watch_logging(db)
        watcher = Watcher(db, config, mode="background", token=payload["token"])
        signal.signal(signal.SIGTERM, lambda signum, frame: watcher.stop())
        signal.signal(signal.SIGINT, lambda signum, frame: watcher.stop())
        return 0 if watcher.run() else 1
    except Exception as exc:
        logging.getLogger("todogc.watch_service").error("Background watcher failed: %s", watch_error(exc, config))
        return 1


if __name__ == "__main__":
    sys.exit(_worker())
