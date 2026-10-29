from __future__ import annotations

from datetime import datetime

from psycopg import Connection
from psycopg.types.json import Jsonb

BACKOFF_SECONDS = [30, 120, 600]


def enqueue(conn: Connection, kind: str, payload: dict, *, priority: int = 100,
            max_attempts: int = 3, run_after: datetime | None = None) -> int:
    row = conn.execute(
        """INSERT INTO jobs (kind, payload, priority, max_attempts, run_after)
           VALUES (%s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
        (kind, Jsonb(payload), priority, max_attempts, run_after),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def claim(conn: Connection, worker_id: str) -> dict | None:
    row = conn.execute(
        """UPDATE jobs SET status='running', locked_by=%s, locked_at=now(),
                           attempts=attempts+1
           WHERE id = (
             SELECT id FROM jobs
             WHERE status='queued' AND run_after <= now()
             ORDER BY priority, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
           RETURNING *""",
        (worker_id,),
    ).fetchone()
    conn.commit()
    return row


def complete(conn: Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', finished_at=now(), locked_by=NULL, locked_at=NULL "
        "WHERE id=%s",
        (job_id,),
    )
    conn.commit()


def fail(conn: Connection, job_id: int, error: str) -> str:
    # 语义：首次执行 + 最多 max_attempts 次重试（计划差异 3）
    row = conn.execute(
        "SELECT attempts, max_attempts FROM jobs WHERE id=%s FOR UPDATE", (job_id,)
    ).fetchone()
    if row["attempts"] > row["max_attempts"]:
        conn.execute(
            "UPDATE jobs SET status='failed', last_error=%s, finished_at=now(), "
            "locked_by=NULL, locked_at=NULL WHERE id=%s",
            (error, job_id),
        )
        conn.commit()
        return "failed"
    delay = BACKOFF_SECONDS[min(row["attempts"] - 1, len(BACKOFF_SECONDS) - 1)]
    conn.execute(
        "UPDATE jobs SET status='queued', last_error=%s, locked_by=NULL, locked_at=NULL, "
        "run_after=now() + make_interval(secs => %s) WHERE id=%s",
        (error, delay, job_id),
    )
    conn.commit()
    return "queued"


def heartbeat(conn: Connection, job_id: int, worker_id: str) -> None:
    conn.execute(
        "UPDATE jobs SET locked_at=now() WHERE id=%s AND locked_by=%s AND status='running'",
        (job_id, worker_id),
    )
    conn.commit()


def reap_stale(conn: Connection, timeout_seconds: int = 600) -> int:
    rows = conn.execute(
        """UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL
           WHERE status='running'
             AND locked_at < now() - make_interval(secs => %s)
           RETURNING id""",
        (timeout_seconds,),
    ).fetchall()
    conn.commit()
    return len(rows)


def queue_depth(conn: Connection) -> int:
    return int(conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')"
    ).fetchone()["n"])
