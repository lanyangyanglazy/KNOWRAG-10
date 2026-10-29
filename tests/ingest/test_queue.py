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


def test_fail_fences_on_worker_ownership(conn):
    job_id = enqueue(conn, "parse", {})
    job = claim(conn, "w1")
    assert job["id"] == job_id

    # 非所有者调用 fail：返回 lost，任务保持 running 且锁仍归 w1
    assert fail(conn, job_id, "late", worker_id="w2") == "lost"
    row = conn.execute(
        "SELECT status, locked_by, last_error FROM jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert row["status"] == "running"
    assert row["locked_by"] == "w1"
    assert row["last_error"] is None

    # 所有者调用 fail 才生效
    assert fail(conn, job_id, "boom", worker_id="w1") == "queued"

    # 不存在的 job_id 返回 lost 而不是抛 TypeError（带不带 worker_id 一致）
    assert fail(conn, 999_999_999, "missing", worker_id="w1") == "lost"
    assert fail(conn, 999_999_999, "missing") == "lost"


def test_complete_fences_on_worker_ownership(conn):
    job_id = enqueue(conn, "parse", {})
    first = claim(conn, "w1")
    assert first["id"] == job_id

    # w1 心跳丢失，任务被回收并由 w2 重新认领
    conn.execute("UPDATE jobs SET locked_at = now() - interval '20 minutes' WHERE id=%s",
                 (job_id,))
    conn.commit()
    assert reap_stale(conn, timeout_seconds=600) == 1
    second = claim(conn, "w2")
    assert second["id"] == job_id

    # w1 迟到的 complete 不得把 w2 手上的任务置为 done
    complete(conn, job_id, worker_id="w1")
    row = conn.execute("SELECT status, locked_by FROM jobs WHERE id=%s", (job_id,)).fetchone()
    assert row["status"] == "running"
    assert row["locked_by"] == "w2"

    # 真正的所有者可以完成
    complete(conn, job_id, worker_id="w2")
    row = conn.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()
    assert row["status"] == "done"


def test_reap_stale_marks_exhausted_attempts_failed(conn):
    job_id = enqueue(conn, "parse", {}, max_attempts=0)
    job = claim(conn, "w1")
    assert job["attempts"] == 1
    conn.execute("UPDATE jobs SET locked_at = now() - interval '20 minutes' WHERE id=%s",
                 (job_id,))
    conn.commit()

    # attempts(1) > max_attempts(0)：回收直接落 failed，不再重放
    assert reap_stale(conn, timeout_seconds=600) == 1
    row = conn.execute(
        "SELECT status, finished_at, last_error, locked_by, locked_at FROM jobs WHERE id=%s",
        (job_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert row["last_error"] is not None
    assert "stale" in row["last_error"].lower() or "timeout" in row["last_error"].lower()
    assert row["locked_by"] is None
    assert row["locked_at"] is None


def test_run_job_skips_dead_letter_when_ownership_lost(conn):
    job_id = enqueue(conn, "parse", {}, max_attempts=0)
    claim(conn, "w1")
    dead = []

    def boom(conn, payload):
        raise RuntimeError("bad")

    # w2 拿着同一 job 字典执行：fail 返回 lost，不触发死信钩子，任务仍归 w1
    assert run_job(conn, {"id": job_id, "kind": "parse", "payload": {}}, {"parse": boom},
                   "w2", {"parse": lambda c, j: dead.append(j["id"])}) is False
    assert dead == []
    row = conn.execute("SELECT status, locked_by FROM jobs WHERE id=%s", (job_id,)).fetchone()
    assert row["status"] == "running"
    assert row["locked_by"] == "w1"


def test_claim_skips_rows_locked_by_other_transaction(conn, db_url):
    enqueue(conn, "parse", {})

    # 另一事务持有唯一 queued 行的行锁：claim 必须跳过而不是阻塞
    with psycopg.connect(db_url, row_factory=dict_row) as other:
        other.execute("BEGIN")
        locked = other.execute(
            "SELECT id FROM jobs WHERE status='queued' FOR UPDATE"
        ).fetchall()
        assert len(locked) == 1
        assert claim(conn, "w1") is None
        other.rollback()

    # 对方释放后即可认领
    claimed = claim(conn, "w1")
    assert claimed is not None
    assert claimed["attempts"] == 1
