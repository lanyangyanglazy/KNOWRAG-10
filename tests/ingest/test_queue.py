from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from ingest.queue import (
    claim, complete, enqueue, fail, heartbeat, queue_depth, reap_stale,
)
from ingest.worker import run_job


def test_enqueue_and_claim_are_skip_locked(conn, db_url):
    enqueue(conn, "parse", {"version_id": "v1"}, priority=10)
    enqueue(conn, "parse", {"version_id": "v2"}, priority=20)
    conn.commit()

    first = claim(conn, "worker-a")
    assert first["payload"]["version_id"] == "v1"
    # conn 已提交，锁释放；另一个连接抢到第二条
    # 注：psycopg 的 conn.info.dsn 不含密码，故用 db_url fixture 建第二条连接
    with psycopg.connect(db_url, row_factory=dict_row) as other:
        second = claim(other, "worker-b")
    assert second["payload"]["version_id"] == "v2"
    assert second["attempts"] == 1


def test_fail_backoff_then_dead_letter(conn):
    job_id = enqueue(conn, "parse", {}, max_attempts=3)
    conn.commit()

    delays = []
    for _ in range(3):
        job = claim(conn, "w")
        status = fail(conn, job["id"], "boom")
        assert status == "queued"
        row = conn.execute("SELECT run_after FROM jobs WHERE id=%s", (job["id"],)).fetchone()
        delays.append((row["run_after"] - datetime.now(timezone.utc)).total_seconds())
        conn.execute("UPDATE jobs SET run_after=now() WHERE id=%s", (job["id"],))
        conn.commit()
    assert delays[0] == pytest.approx(30, abs=5)
    assert delays[1] == pytest.approx(120, abs=5)
    assert delays[2] == pytest.approx(600, abs=5)

    job = claim(conn, "w")
    assert fail(conn, job["id"], "boom") == "failed"
    row = conn.execute("SELECT status, last_error, finished_at FROM jobs WHERE id=%s",
                       (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["last_error"] == "boom"
    assert row["finished_at"] is not None


def test_reap_stale_requeues(conn):
    enqueue(conn, "parse", {})
    conn.commit()
    job = claim(conn, "w")
    conn.execute("UPDATE jobs SET locked_at = now() - interval '20 minutes' WHERE id=%s",
                 (job["id"],))
    conn.commit()
    assert reap_stale(conn, timeout_seconds=600) == 1
    row = conn.execute("SELECT status, locked_by FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    assert row["status"] == "queued"
    assert row["locked_by"] is None


def test_run_job_marks_done_and_calls_dead_letter(conn):
    job_id = enqueue(conn, "parse", {"version_id": "v1"})
    conn.commit()
    job = claim(conn, "w")
    calls = []

    def ok(conn, payload):
        calls.append(payload["version_id"])

    assert run_job(conn, job, {"parse": ok}, "w") is True
    assert calls == ["v1"]
    assert conn.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()["status"] == "done"

    job_id2 = enqueue(conn, "parse", {"version_id": "v2"}, max_attempts=0)
    conn.commit()
    job2 = claim(conn, "w")

    def boom(conn, payload):
        raise RuntimeError("bad")

    dead = []
    assert run_job(conn, job2, {"parse": boom}, "w",
                   {"parse": lambda c, j: dead.append(j["id"])}) is False
    assert dead == [job_id2]
    assert conn.execute("SELECT status FROM jobs WHERE id=%s", (job_id2,)).fetchone()["status"] == "failed"


def test_heartbeat_and_depth(conn):
    enqueue(conn, "parse", {})
    conn.commit()
    assert queue_depth(conn) == 1
    job = claim(conn, "w")
    conn.execute("UPDATE jobs SET locked_at = now() - interval '5 minutes' WHERE id=%s",
                 (job["id"],))
    conn.commit()
    heartbeat(conn, job["id"], "w")
    row = conn.execute("SELECT locked_at FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    assert (datetime.now(timezone.utc) - row["locked_at"]).total_seconds() < 10
    complete(conn, job["id"])
    assert queue_depth(conn) == 0
