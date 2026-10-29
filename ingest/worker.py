from __future__ import annotations

import threading
import time
from typing import Callable

from psycopg import Connection

from ingest.db import connection
from ingest.queue import claim, complete, fail, heartbeat, reap_stale

Handler = Callable[[Connection, dict], None]
Hook = Callable[[Connection, dict], None]


def run_job(conn: Connection, job: dict, handlers: dict[str, Handler],
            worker_id: str, dead_letter_hooks: dict[str, Hook] | None = None) -> bool:
    handler = handlers.get(job["kind"])
    if handler is None:
        fail(conn, job["id"], f"unknown job kind: {job['kind']}")
        return False
    try:
        handler(conn, job["payload"])
    except Exception as exc:  # noqa: BLE001 - 失败必须落库并重试
        conn.rollback()
        status = fail(conn, job["id"], f"{type(exc).__name__}: {exc}")
        if status == "failed" and dead_letter_hooks and job["kind"] in dead_letter_hooks:
            dead_letter_hooks[job["kind"]](conn, job)
        return False
    complete(conn, job["id"])
    return True


def _start_heartbeat(job_id: int, worker_id: str) -> threading.Event:
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(30):
            try:
                with connection() as hb:
                    heartbeat(hb, job_id, worker_id)
            except Exception:  # noqa: BLE001 - 心跳失败交给回收机制
                pass

    threading.Thread(target=beat, daemon=True).start()
    return stop


def run_forever(handlers: dict[str, Handler], *, worker_id: str,
                poll_seconds: float = 1.0,
                dead_letter_hooks: dict[str, Hook] | None = None) -> None:
    while True:
        with connection() as conn:
            reap_stale(conn)
            job = claim(conn, worker_id)
        if job is None:
            time.sleep(poll_seconds)
            continue
        stop = _start_heartbeat(job["id"], worker_id)
        try:
            with connection() as conn:
                run_job(conn, job, handlers, worker_id, dead_letter_hooks)
        finally:
            stop.set()
