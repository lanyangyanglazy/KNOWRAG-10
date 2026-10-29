"""任务查询与 SSE 进度。

任务可见性沿用管理台口径：管理员全量可见，其余用户只能看到本部门文档的任务；
判断文档归属时按 payload 里的 document_id / version_id 反查，避免任务列表成为
跨部门文档标题/ID 的旁路。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from psycopg import Connection

from app.deps import current_user, get_db
from app.users import User
from ingest import repository as repo

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# SSE 轮询上限：600 次 × 1s = 10 分钟，超时后由前端重新连接。
POLL_LIMIT = 600
POLL_INTERVAL_S = 1.0


def _job_document(conn: Connection, job: dict) -> dict | None:
    payload = job["payload"] or {}
    if payload.get("document_id"):
        return repo.get_document(conn, payload["document_id"])
    if payload.get("version_id"):
        version = repo.get_version(conn, payload["version_id"])
        if version is not None:
            return repo.get_document(conn, version["document_id"])
    return None


def _can_watch(conn: Connection, job: dict, user: User) -> bool:
    if "admin" in user.roles:
        return True
    doc = _job_document(conn, job)
    return doc is not None and doc["dept"] == user.dept


def _load_job(conn: Connection, job_id: int, user: User) -> dict:
    job = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if job is None or not _can_watch(conn, job, user):
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.get("/{job_id}")
def job_status(job_id: int, user: User = Depends(current_user),
               conn: Connection = Depends(get_db)) -> dict:
    return dict(_load_job(conn, job_id, user))


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{job_id}/events")
def job_events(job_id: int, user: User = Depends(current_user)) -> StreamingResponse:
    # 鉴权用一次性短连接，绝不通过 Depends(get_db) 把请求作用域连接带进流：
    # SSE 可存活 10 分钟，期间每次轮询还要再借一个，几路观众就能耗尽连接池。
    from ingest.db import connection

    with connection() as conn:
        _load_job(conn, job_id, user)

    # 异步生成器：客户端断开会取消 await，循环随之停止；同步 DB 调用很短，
    # P0 允许直接放在事件循环上。
    async def stream():
        last = None
        for _ in range(POLL_LIMIT):
            with connection() as c:
                job = c.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            if job is None:
                yield _event("job.failed", {"job_id": job_id, "status": "missing"})
                return
            state = job["status"]
            if state != last:
                name = {"done": "job.done", "failed": "job.failed"}.get(
                    state, "job.progress")
                yield _event(name, {
                    "job_id": job_id, "kind": job["kind"], "status": state,
                    "attempts": job["attempts"], "last_error": job["last_error"],
                })
                last = state
            if state in ("done", "failed"):
                return
            await asyncio.sleep(POLL_INTERVAL_S)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
