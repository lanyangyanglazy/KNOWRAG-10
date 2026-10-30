from __future__ import annotations

import threading
from contextlib import nullcontext

import psycopg
import pytest

from ingest import worker
from ingest.queue import enqueue


def test_run_once_drains_queue_in_priority_order(conn, monkeypatch):
    seen: list[int] = []

    def handler(db, payload):
        seen.append(payload["n"])

    enqueue(conn, "demo", {"n": 1}, priority=10)
    enqueue(conn, "demo", {"n": 2}, priority=20)
    conn.commit()

    monkeypatch.setattr(worker, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(worker, "_start_heartbeat",
                        lambda job_id, worker_id: threading.Event())

    assert worker.run_once({"demo": handler}, worker_id="test") == 2
    assert seen == [1, 2]
    assert conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE status='done'"
    ).fetchone()["n"] == 2


def test_run_once_returns_zero_on_empty_queue(conn, monkeypatch):
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(conn))
    assert worker.run_once({}, worker_id="test") == 0


def test_run_once_unknown_kind_goes_dead_letter(conn, monkeypatch):
    enqueue(conn, "nobody", {"x": 1}, max_attempts=0)
    conn.commit()
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(worker, "_start_heartbeat",
                        lambda job_id, worker_id: threading.Event())

    assert worker.run_once({}, worker_id="test") == 1
    row = conn.execute("SELECT status, last_error FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert "unknown job kind" in row["last_error"]


class FakeStop:
    """假停止位：按 wait 次数判定「收到停机」，并记录每次退避时长。"""

    def __init__(self, max_waits: int):
        self.max_waits = max_waits
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return len(self.waits) >= self.max_waits

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return self.is_set()


def _failing_connection(monkeypatch, error=psycopg.OperationalError("db down")):
    def connection():
        raise error

    monkeypatch.setattr(worker, "connection", connection)


def test_run_forever_backs_off_instead_of_dying_when_db_is_down(conn, monkeypatch):
    # 抖动（psycopg.Error）必须退避重试：一次连接失败就让 worker 退出，
    # 在政务现场等于数据库重启后整条摄取链停摆。
    _failing_connection(monkeypatch)
    monkeypatch.setattr(worker, "MAX_BACKOFF_SECONDS", 8.0)
    stop = FakeStop(max_waits=5)

    worker.run_forever({}, worker_id="test", poll_seconds=1.0, stop=stop)

    assert stop.waits == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_run_forever_resets_backoff_after_a_healthy_poll(conn, monkeypatch):
    script = ["fail", "ok", "fail"]

    class _Ctx:
        def __init__(self, kind: str):
            self.kind = kind

        def __enter__(self):
            if self.kind == "fail":
                raise psycopg.OperationalError("db down")
            return conn

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(worker, "connection",
                        lambda: _Ctx(script.pop(0) if script else "ok"))
    monkeypatch.setattr(worker, "reap_stale", lambda db: None)
    monkeypatch.setattr(worker, "claim", lambda db, worker_id: None)
    stop = FakeStop(max_waits=3)

    worker.run_forever({}, worker_id="test", poll_seconds=1.0, stop=stop)

    # 成功一轮后必须回到基础间隔；若不重置，第三段会是 2.0。
    assert stop.waits == [1.0, 1.0, 1.0]


def test_run_forever_exits_without_polling_when_already_stopped(monkeypatch):
    def connection():
        raise AssertionError("must not touch the database")

    monkeypatch.setattr(worker, "connection", connection)
    stop = threading.Event()
    stop.set()

    worker.run_forever({}, worker_id="test", stop=stop)


def test_main_rejects_bad_poll_with_exit_code_2():
    for value in ("0", "-1", "nan", "inf"):
        with pytest.raises(SystemExit) as excinfo:
            worker.main(["--poll", value])
        assert excinfo.value.code == 2, value
