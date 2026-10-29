from __future__ import annotations

import threading
from contextlib import nullcontext

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
