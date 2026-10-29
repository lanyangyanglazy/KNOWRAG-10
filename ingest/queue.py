from __future__ import annotations

from datetime import datetime

from psycopg import Connection
from psycopg.types.json import Jsonb

BACKOFF_SECONDS = [30, 120, 600]

# reap_stale 判定重试耗尽时写入 last_error 的说明，便于运维定位崩溃循环
STALE_TIMEOUT_ERROR = "stale timeout: worker heartbeat expired and attempts exhausted"


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


def complete(conn: Connection, job_id: int, *, worker_id: str | None = None) -> None:
    """标记任务完成。

    worker_id 非 None 时只更新 status='running' 且 locked_by=worker_id 的行：
    已被 reap_stale 回收并由其他 worker 重新认领的任务不会被迟到的完成调用覆盖。
    UPDATE 自身的行锁 + 谓词（READ COMMITTED 下锁后重检）即所有权栅栏。
    """
    if worker_id is None:
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=now(), locked_by=NULL, locked_at=NULL "
            "WHERE id=%s",
            (job_id,),
        )
    else:
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=now(), locked_by=NULL, locked_at=NULL "
            "WHERE id=%s AND status='running' AND locked_by=%s",
            (job_id, worker_id),
        )
    conn.commit()


def fail(conn: Connection, job_id: int, error: str, *,
         worker_id: str | None = None) -> str:
    """记录任务失败并返回后续状态：queued | failed | lost。

    - queued：未耗尽重试次数，已按退避重新排队；
    - failed：重试耗尽进入终态，调用方负责触发死信钩子；
    - lost：任务不存在（含 job_id 已删除），或 worker_id 非 None 时该行已不满足
      status='running' 且 locked_by=worker_id（已被回收或由其他 worker 接管），
      此时不做任何修改。
    """
    # 语义：首次执行 + 最多 max_attempts 次重试（计划差异 3）
    if worker_id is None:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id=%s FOR UPDATE", (job_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs "
            "WHERE id=%s AND status='running' AND locked_by=%s FOR UPDATE",
            (job_id, worker_id),
        ).fetchone()
    if row is None:
        # 无匹配行：未取得行锁也未写入，结束只读事务，交由调用方按 lost 处理
        conn.rollback()
        return "lost"
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
    """回收心跳超时（locked_at 过旧）的 running 任务。

    未耗尽重试次数的重新排队；已耗尽（attempts > max_attempts）的直接落 failed，
    写入 finished_at 与 stale 超时说明，避免崩溃任务被无限重放。
    返回本轮处理的任务数（重新排队 + 置为 failed）。
    """
    rows = conn.execute(
        """UPDATE jobs
           SET status = CASE WHEN attempts > max_attempts THEN 'failed' ELSE 'queued' END,
               last_error = CASE WHEN attempts > max_attempts
                                 THEN %s ELSE last_error END,
               finished_at = CASE WHEN attempts > max_attempts
                                  THEN now() ELSE finished_at END,
               locked_by = NULL,
               locked_at = NULL
           WHERE status='running'
             AND locked_at < now() - make_interval(secs => %s)
           RETURNING id""",
        (STALE_TIMEOUT_ERROR, timeout_seconds),
    ).fetchall()
    conn.commit()
    return len(rows)


def queue_depth(conn: Connection) -> int:
    return int(conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')"
    ).fetchone()["n"])
