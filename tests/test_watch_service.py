"""Actual detached workers and CLI management, with notifications confined to localhost."""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from todogc.cli import main
from todogc.config import AppConfig
from todogc.notify import NotifyError
from todogc.scheduling import next_weekly_due
from todogc.store import Store
from todogc.watch_service import (
    configure_watch_logging, format_service_status, log_path, read_watch_logs,
    service_status, start_background, stop_watch,
)
from todogc.watcher import Watcher, public_watch_settings


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for watcher state")


@pytest.fixture
def notification_server():
    state = {"fail": False, "messages": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["messages"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(503 if state["fail"] else 200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def managed(tmp_path, notification_server):
    server, notifications = notification_server
    db = tmp_path / "managed.db"
    config = tmp_path / "managed.toml"
    config.write_text('[notify]\nntfy_enabled=true\nntfy_topic="local-test"\n'
                      f'ntfy_server="{server}"\nntfy_token="fake-private-token"\n'
                      '[watch]\npoll_seconds=1\ndigest_seconds=999999\nweekly_review_enabled=false\n')
    with Store(db) as store:
        store.add_task("A private goal", is_root=True)
    command = [sys.executable, "-m", "todogc.cli", "--db", str(db), "--config", str(config)]
    env = {key: value for key, value in os.environ.items() if not key.startswith("TODOGC_")}

    def cli(*args, cwd=None):
        return subprocess.run([*command, *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=25)

    try:
        yield db, config, notifications, cli
    finally:
        stop_watch(str(db), AppConfig(), timeout=5)


def test_status_logs_and_stop_work_without_config_or_creating_runtime_files(tmp_path, capsys):
    config = tmp_path / "broken.toml"
    config.write_text("not valid TOML")
    db = tmp_path / "absent.db"
    args = ["--db", str(db), "--config", str(config), "watch"]
    assert main([*args, "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "stopped" and not status["current_config_available"]
    assert main([*args, "logs"]) == 0
    assert "No watcher logs" in capsys.readouterr().out
    assert main([*args, "stop"]) == 0
    assert not db.exists() and not log_path(str(db)).parent.exists()
    assert main([*args, "test"]) == 2  # sending still requires valid configuration


def test_status_does_not_migrate_an_old_database(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE task(id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO task VALUES (1, 'Existing task')")
    before = db.read_bytes()
    assert service_status(str(db), AppConfig())["state"] == "stopped"
    assert db.read_bytes() == before
    assert not (tmp_path / "old.db.bak").exists()


def test_disabled_start_and_test_leave_no_database_or_log(tmp_path, capsys):
    db = tmp_path / "disabled.db"
    for action in ("start", "test"):
        assert main(["--db", str(db), "--config", str(tmp_path / "missing.toml"), "watch", action]) == 1
        assert "ntfy" in capsys.readouterr().err
    assert not db.exists() and not log_path(str(db)).parent.exists()


def test_runtime_settings_schedule_delivery_and_dst_are_visible(tmp_path):
    now = datetime(2026, 3, 7, 0, tzinfo=timezone.utc)
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic="local", ntfy_token="fake-secret",
                    watch_poll_seconds=23, watch_weekly_review_timezone="America/New_York")
    db = str(tmp_path / "status.db")
    with Store(db) as store:
        assert store.try_acquire_watch_lock(os.getpid(), now, stale_after=timedelta(minutes=5),
                                            token="instance", mode="background", settings=json.dumps(public_watch_settings(cfg)))
        store.watch_meta_set("watch_last_check_at", now.isoformat())
        store.watch_meta_set("weekly_review:America/New_York:4:17:00:2026-03-06T22:00:00+00:00:sent", now.isoformat())
    status = service_status(db, AppConfig(watch_poll_seconds=90), now=now)
    assert status["state"] == "running" and status["mode"] == "background"
    assert status["settings"]["watch_poll_seconds"] == 23
    assert status["next_check_at"] == (now + timedelta(seconds=23)).isoformat()
    assert status["next_weekly_at"] == "2026-03-13T21:00:00+00:00"
    assert status["pending_weekly_at"] is None and status["last_weekly_sent_at"] == now.isoformat()
    assert "Friday 17:00" in format_service_status(status)
    assert "fake-secret" not in json.dumps(status)
    assert "ntfy_token" not in status["settings"] and "ntfy_server" not in status["settings"]
    at_boundary = datetime(2026, 3, 13, 21, tzinfo=timezone.utc)
    assert next_weekly_due(at_boundary, timezone_name="America/New_York") == at_boundary + timedelta(days=7)


def test_logs_tail_spans_rotations_without_creating_database(tmp_path):
    db = str(tmp_path / "logs.db")
    path = log_path(db)
    path.parent.mkdir()
    for suffix, content in ((".3", "old\n"), (".2", "middle\n"), (".1", "recent\n"), ("", "latest\nlast\n")):
        path.with_name(path.name + suffix).write_text(content)
    assert read_watch_logs(db, lines=3) == "recent\nlatest\nlast"
    assert not (tmp_path / "logs.db").exists()


def test_lock_acquisition_is_atomic_even_for_two_watchers_in_one_process(tmp_path, now):
    db = tmp_path / "lock.db"
    with Store(db):
        pass
    barrier = threading.Barrier(2)

    def acquire(token):
        with Store(db) as store:
            barrier.wait(timeout=5)
            return token, store.try_acquire_watch_lock(123, now, stale_after=timedelta(minutes=5), token=token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, ("first", "second")))
    assert sum(ok for token, ok in results) == 1
    winner = next(token for token, ok in results if ok)
    loser = next(token for token, ok in results if not ok)
    with Store(db) as store:
        assert not store.heartbeat_watch_lock(123, now, token=loser)
        assert not store.request_watch_stop(loser)
        store.release_watch_lock(123, token=loser)
        assert store.watch_meta_get("watch_lock_token") == winner
        assert store.request_watch_stop(winner)
        store.release_watch_lock(123, token=winner)
        assert store.watch_meta_get("watch_lock_pid") is None


def test_delivery_failures_are_persisted_redacted_and_then_recover(store, now, caplog):
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic="test", ntfy_token="fake-private-token", watch_weekly_review_enabled=False)

    class Channel:
        fail = True

        def send(self, *args, **kwargs):
            if self.fail:
                raise NotifyError(f"Rejected fake-private-token at {cfg.ntfy_server}")

    watcher = Watcher(":memory:", cfg)
    channel = Channel()
    watcher._channel = channel
    with caplog.at_level(logging.WARNING, logger="todogc.watcher"):
        watcher._run_once(store, now)
    attempt = json.loads(store.watch_meta_get("watch_last_notification"))
    assert not attempt["ok"] and attempt["kind"] == "digest"
    assert "fake-private-token" not in attempt["error"] + caplog.text
    assert store.watch_meta_get("last_digest_at") is None
    channel.fail = False
    watcher._run_once(store, now + timedelta(minutes=1))
    assert json.loads(store.watch_meta_get("watch_last_notification"))["ok"]
    assert store.watch_meta_get("watch_last_notification_success_at") is not None
    assert store.watch_meta_get("watch_last_notification_failure_at") is not None


def test_background_lifecycle_effective_settings_test_logs_and_broken_config(managed, tmp_path):
    db, config, notifications, cli = managed
    result = cli("--heap-max", "77", "--gc-mode", "gentle", "watch", "start")
    assert result.returncode == 0, result.stderr
    wait_for(lambda: notifications["messages"])
    status = json.loads(cli("watch", "status", "--json").stdout)
    assert status["state"] == "running" and status["mode"] == "background"
    assert status["settings"]["heap_max"] == 77 and status["settings"]["gc_mode"] == "gentle"
    pid = status["pid"]
    wait_for(lambda: service_status(str(db), AppConfig())["last_check_finished_at"])
    assert cli("watch", "start").returncode == 0
    assert json.loads(cli("watch", "status", "--json").stdout)["pid"] == pid
    assert cli("add", "Added while running", "--root").returncode == 0
    assert cli("watch", "test").returncode == 0
    assert any(message["title"] == "TodoGC · Test notification" for message in notifications["messages"])
    with Store(db) as store:
        assert store.saved_reviews() == [] and store.gc_runs() == []
        assert all(store.view_count(tid) == store.refcount(tid) == 0 for tid in (1, 2))
    config.write_text("broken TOML")
    other = tmp_path / "other"
    other.mkdir()
    status = json.loads(cli("watch", "status", "--json", cwd=other).stdout)
    assert status["settings"]["watch_poll_seconds"] == 1 and not status["current_config_available"]
    logs = cli("watch", "logs", "--lines", "100", cwd=other)
    assert logs.returncode == 0 and "Watcher started" in logs.stdout
    assert "fake-private-token" not in logs.stdout + json.dumps(status)
    assert cli("watch", "stop", cwd=other).returncode == 0
    assert json.loads(cli("watch", "status", "--json").stdout)["state"] == "stopped"
    assert "Watcher stopped" in cli("watch", "logs").stdout


def test_two_simultaneous_start_commands_use_one_background_owner(managed):
    db, config, notifications, cli = managed
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cli("watch", "start"), range(2)))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    status = json.loads(cli("watch", "status", "--json").stdout)
    assert status["state"] == "running" and status["mode"] == "background"
    wait_for(lambda: notifications["messages"])
    assert len(notifications["messages"]) == 1


def test_stop_can_target_a_web_thread_without_terminating_its_process(managed):
    db, config, notifications, cli = managed
    from todogc.config import load_config
    watcher = Watcher(str(db), load_config(config, environ={}), mode="web")
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: service_status(str(db), AppConfig())["state"] == "running")
        assert cli("watch", "start").returncode == 0
        status = json.loads(cli("watch", "status", "--json").stdout)
        assert status["mode"] == "web" and status["pid"] == os.getpid()
        assert cli("watch", "stop").returncode == 0
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert service_status(str(db), AppConfig())["state"] == "stopped"
    finally:
        watcher.stop()
        thread.join(timeout=5)


def test_failed_test_notification_is_not_a_check_or_weekly_review(managed):
    db, config, notifications, cli = managed
    notifications["fail"] = True
    result = cli("watch", "test")
    assert result.returncode == 1 and "503" in result.stderr
    status = json.loads(cli("watch", "status", "--json").stdout)
    assert status["state"] == "stopped" and status["last_check_at"] is None
    assert status["last_notification"]["kind"] == "test" and not status["last_notification"]["ok"]
    assert "503" in cli("watch", "logs").stdout
    with Store(db) as store:
        assert not store.saved_reviews() and store.view_count(1) == store.refcount(1) == 0
    notifications["fail"] = False
    assert cli("watch", "test").returncode == 0
    status = json.loads(cli("watch", "status", "--json").stdout)
    assert status["last_notification"]["ok"] and status["last_notification_success_at"]


def test_stop_then_restart_creates_a_new_instance_without_repeating_digest(managed):
    db, config, notifications, cli = managed
    assert cli("watch", "start").returncode == 0
    wait_for(lambda: notifications["messages"])
    with Store(db) as store:
        original = store.watch_meta_get("watch_lock_token")
    assert cli("watch", "stop").returncode == 0
    assert cli("watch", "start").returncode == 0
    with Store(db) as store:
        assert store.watch_meta_get("watch_lock_token") != original
    assert len(notifications["messages"]) == 1


@pytest.mark.skipif(os.name != "posix", reason="Immediate exited-process detection uses POSIX existence probes")
def test_abrupt_exit_can_be_detected_and_restarted_without_waiting_for_stale_timeout(managed):
    from todogc import watch_service
    from todogc.config import load_config

    db, config, notifications, cli = managed
    cfg = load_config(config, environ={})
    status = start_background(str(db), cfg)
    assert status["state"] == "running"
    wait_for(lambda: notifications["messages"])
    with Store(db) as store:
        token = store.watch_meta_get("watch_lock_token")
    child = watch_service._children[token]
    # Kill and reap only the isolated worker we just created, simulating a crash.
    child.kill()
    child.wait(timeout=5)
    assert service_status(str(db), cfg)["state"] == "unresponsive"
    fresh = start_background(str(db), cfg)
    assert fresh["state"] == "running"
    with Store(db) as store:
        assert store.watch_meta_get("watch_lock_token") != token
    stop_watch(str(db), cfg)
    assert token not in watch_service._children


def test_old_instance_cannot_renew_release_or_stop_a_replacement_with_the_same_pid(store, now):
    assert store.try_acquire_watch_lock(123, now, token="old", stale_after=timedelta(seconds=60))
    later = now + timedelta(seconds=61)
    assert store.try_acquire_watch_lock(123, later, token="new", stale_after=timedelta(seconds=60))
    assert not store.heartbeat_watch_lock(123, later, token="old")
    assert not store.request_watch_stop("old")
    store.release_watch_lock(123, token="old")
    assert store.watch_meta_get("watch_lock_token") == "new"


def test_test_notification_does_not_consume_due_snoozes(managed):
    db, config, notifications, cli = managed
    now = datetime.now(timezone.utc)
    with Store(db) as store:
        task = store.add_task("Due task", root_id=1, ts=now - timedelta(hours=1))
        store.snooze_task(task.id, now - timedelta(seconds=1), ts=now - timedelta(minutes=1))
        before = store.touches(task.id)
    assert cli("watch", "test").returncode == 0
    assert len(notifications["messages"]) == 1
    with Store(db) as store:
        assert [task.id for task in store.due_snoozes(now)] == [2]
        assert store.touches(2) == before
        assert not store.watch_states() and not store.saved_reviews()


def test_logs_rotate_and_runtime_directory_is_private(tmp_path):
    db = str(tmp_path / "rotating.db")
    logger = logging.getLogger("todogc")
    previous_level = logger.level
    configure_watch_logging(db)
    handler = next(handler for handler in logger.handlers if getattr(handler, "_todogc_watch", False))
    handler.maxBytes = 160
    try:
        for number in range(12):
            logger.info("Rotation check %s", number)
        handler.flush()
        path = log_path(db)
        assert path.with_name(path.name + ".1").exists()
        assert "Rotation check 11" in read_watch_logs(db, lines=1)
        if os.name == "posix":
            assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)


def test_worker_failure_is_visible_in_status(tmp_path, monkeypatch):
    db = str(tmp_path / "failed.db")
    cfg = AppConfig(ntfy_enabled=True, ntfy_topic="test", ntfy_token="fake-secret")
    watcher = Watcher(db, cfg)

    def fail(store, now):
        raise ValueError("Check failed with fake-secret")

    monkeypatch.setattr(watcher, "_run_once", fail)
    with pytest.raises(ValueError):
        watcher.run()
    status = service_status(db, cfg)
    assert watcher.finished and status["state"] == "failed" and status["pid"] is None
    assert "fake-secret" not in status["last_check_error"]
