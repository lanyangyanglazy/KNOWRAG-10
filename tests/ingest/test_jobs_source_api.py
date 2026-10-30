from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ingest import repository as repo
from ingest.queue import enqueue

# health 在所有结果分支都应返回同一套键，消费方不必按状态分支取字段。
HEALTH_KEYS = {"ok", "docs", "chunks", "skipped_docs", "skipped_files",
               "max_distance", "embed_model", "db", "queue_depth"}


def seed_version(db_url: str, *, dept: str = "财务处",
                 classification: int | None = 1) -> tuple[str, str]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        doc = repo.create_document(conn, title="预算", dept=dept,
                                   classification=classification,
                                   effective_date=None, created_by="wang")
        version = repo.create_version(
            conn, document_id=doc["id"], version_no=2, sha256="c" * 64,
            filename="预算.xlsx", mime="application/octet-stream", size_bytes=10,
            storage_path="cc/" + "c" * 64, parser="xlsx", parser_version="0.1.0")
        conn.commit()
        return doc["id"], version["id"]


def seed_chunk(db_url: str, doc_id: str, version_id: str, *,
               classification: int = 1, active: bool = True) -> str:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        conn.execute(
            """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type, content,
                   content_tokens, embed_model, dept, classification, metadata,
                   chunk_hash, active)
               VALUES (%s, %s, 7, 'table_rows', '第 3-4 行：综合处 100',
                       '第 3-4 行 综合处 100', 'bge-m3', '财务处', %s, %s, %s, %s)""",
            (doc_id, version_id, classification,
             Jsonb({"chunk_type": "table_rows", "sheet": "2025预算",
                    "cell_range": "A3:B4", "row_range": "3-4"}),
             "d" * 64, active),
        )
        conn.execute("UPDATE documents SET status='active', current_version_id=%s WHERE id=%s",
                     (version_id, doc_id))
        conn.commit()
    return f"{doc_id}@v2#7"


def chunk_url(chunk_id: str) -> str:
    # chunk_id 含 `#`，直接拼 URL 会被 httpx 当成 fragment 丢掉，必须百分号编码。
    return f"/api/chunks/{quote(chunk_id, safe='')}/source"


def test_job_status_and_sse(api):
    make, db_url = api
    _, version_id = seed_version(db_url)
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        job_id = enqueue(conn, "parse", {"version_id": str(version_id)}, priority=50)
    client = make("wang")

    status = client.get(f"/api/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "queued"

    # SSE 对未完成任务会持续推送，测试只验证终态事件立即返回。
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        conn.execute("UPDATE jobs SET status='done', finished_at=now() WHERE id=%s", (job_id,))
        conn.commit()
    done = client.get(f"/api/jobs/{job_id}/events")
    assert done.status_code == 200
    assert "event: job.done" in done.text
    assert '"status": "done"' in done.text


def test_job_visibility_is_department_scoped(api):
    make, db_url = api
    _, version_id = seed_version(db_url)
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        job_id = enqueue(conn, "parse", {"version_id": str(version_id)}, priority=50)

    li = make("li")
    assert li.get(f"/api/jobs/{job_id}").status_code == 404
    chen = make("chen")
    assert chen.get(f"/api/jobs/{job_id}").status_code == 200


def seed_job(db_url: str, *, dept: str = "综合处",
             classification: int = 2) -> tuple[str, int]:
    """造一个挂在部门文档上的任务，返回 (document_id, job_id)。"""
    doc_id, version_id = seed_version(db_url, dept=dept, classification=classification)
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        return doc_id, enqueue(conn, "embed", {"version_id": str(version_id)}, priority=50)


def test_secret_job_hidden_from_low_clearance_same_dept(api):
    make, db_url = api
    _, job_id = seed_job(db_url)

    # zhang：综合处、max_level=1、非 admin —— 同部门但密级不足，按不存在处理。
    zhang = make("zhang")
    assert zhang.get(f"/api/jobs/{job_id}").status_code == 404
    assert zhang.get(f"/api/jobs/{job_id}/events").status_code == 404

    # 阳性对照：同部门、同角色的 L2 非管理员必须可见，
    # 证明 404 来自密级不足而不是部门或角色判断。
    from app.routers import jobs as jobs_router
    from app.users import DEMO_USERS, User

    stronger = User("liu", "刘洋", "综合处", max_level=2, role="处长",
                    roles=("uploader", "reviewer"))
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
        assert jobs_router._can_watch(conn, job, stronger) is True
        assert jobs_router._can_watch(conn, job, DEMO_USERS["zhang"]) is False


def test_public_job_visible_cross_department(api):
    make, db_url = api
    _, job_id = seed_job(db_url, classification=0)

    li = make("li")
    resp = li.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "embed"


def test_unclassified_job_hidden_from_non_admin(api):
    make, db_url = api
    doc_id, job_id = seed_job(db_url, classification=1)
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        # 密级缺失（NULL）必须 fail-closed：只有管理员能看。
        conn.execute("UPDATE documents SET classification=NULL WHERE id=%s", (doc_id,))
        conn.commit()

    zhang = make("zhang")
    assert zhang.get(f"/api/jobs/{job_id}").status_code == 404
    chen = make("chen")
    assert chen.get(f"/api/jobs/{job_id}").status_code == 200


def test_job_events_does_not_hold_request_db_connection():
    # SSE 若通过 Depends(get_db) 持有请求作用域连接，该连接会一直占到流结束；
    # 加上每次轮询再借一个，几路观众就能打满小连接池。
    from app.routers import jobs

    route = next(r for r in jobs.router.routes if r.path == "/api/jobs/{job_id}/events")
    deps = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
    assert "get_db" not in deps
    assert "current_user" in deps


def test_chunk_source_returns_locator_and_rechecks_acl(api):
    make, db_url = api
    doc_id, version_id = seed_version(db_url)
    chunk_id = seed_chunk(db_url, doc_id, version_id)

    wang = make("wang")
    resp = wang.get(chunk_url(chunk_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["locator"]["sheet"] == "2025预算"
    assert body["locator"]["cell_range"] == "A3:B4"
    assert body["filename"] == "预算.xlsx"

    zhang = make("zhang")  # 综合处：内部级不可见
    assert zhang.get(chunk_url(chunk_id)).status_code == 404
    assert wang.get("/api/chunks/not-a-chunk-id/source").status_code == 404

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        conn.execute("UPDATE chunks SET active=false")
        conn.commit()
    assert wang.get(chunk_url(chunk_id)).status_code == 404


def test_chunk_source_invalid_ids_are_404_not_500(api):
    make, db_url = api
    doc_id, version_id = seed_version(db_url)
    seed_chunk(db_url, doc_id, version_id)  # 置文档 active，确保请求走到 id 解析而非状态检查
    wang = make("wang")

    # 36 个连字符能通过旧的 [0-9a-f-]{36} 正则，但不是合法 UUID：
    # 直接拼进 SQL 会被 psycopg 以 InvalidTextRepresentation 拒绝 → 500。
    bare = "-" * 36
    resp = wang.get(chunk_url(f"{bare}@v1#1"))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "切片不存在"

    # 版本号不限长时 int() 在超长数字上抛 ValueError → 500。
    huge = f"{doc_id}@v{'9' * 5000}#7"
    assert wang.get(chunk_url(huge)).status_code == 404


def test_public_chunk_is_visible_cross_department(api):
    make, db_url = api
    doc_id, version_id = seed_version(db_url, classification=0)
    chunk_id = seed_chunk(db_url, doc_id, version_id, classification=0)

    zhang = make("zhang")
    assert zhang.get(chunk_url(chunk_id)).status_code == 200


def test_health_reports_db_and_queue(api, monkeypatch):
    make, db_url = api
    client = make("wang")
    from app import server

    class _Service:
        def stats(self):
            # health() 直接透传 stats 的 embed_model：faiss=bge-m3、pg=INGEST_EMBED_MODEL
            return {"docs": 0, "chunks": 0, "embed_model": "bge-m3"}

    # 队列里先放一个 queued 任务：恒返回 0 的实现必须在这里失败。
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        enqueue(conn, "parse",
                {"version_id": "00000000-0000-0000-0000-000000000000"}, priority=50)

    monkeypatch.setattr(server, "_service", _Service())
    monkeypatch.setattr(server, "_service_error", None)
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["db"] == "ok"
    assert body["queue_depth"] == 1
    assert body["embed_model"]
    # 既有字段必须保留，避免前端/探针消费方回归。
    assert body["skipped_docs"] == 0
    assert body["skipped_files"] == []
    assert HEALTH_KEYS <= set(body)


def test_health_without_db_keeps_stable_keys(api, monkeypatch):
    make, _ = api
    client = make("wang")
    from app import server
    import ingest.config as ingest_config

    class _Service:
        def stats(self):
            # 故意不带 embed_model / load_issues：缺键不能让 health 变 500。
            return {"docs": 1, "chunks": 2}

    class _Settings:
        db_url = ""

    monkeypatch.setattr(server, "_service", _Service())
    monkeypatch.setattr(server, "_service_error", None)
    monkeypatch.setattr(ingest_config, "get_settings", lambda: _Settings())
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["db"] is None
    assert body["queue_depth"] is None
    assert body["embed_model"] is None
    assert body["skipped_docs"] == 0
    assert body["skipped_files"] == []
    assert HEALTH_KEYS <= set(body)


def test_health_returns_503_when_db_probe_fails(api, monkeypatch):
    make, _ = api
    client = make("wang")
    from app import server
    import ingest.db as ingest_db

    class _Service:
        def stats(self):
            return {"docs": 0, "chunks": 0, "embed_model": "bge-m3"}

    @contextmanager
    def broken_connection():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(server, "_service", _Service())
    monkeypatch.setattr(server, "_service_error", None)
    monkeypatch.setattr(ingest_db, "connection", broken_connection)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["db"] == "error: RuntimeError"
    # 探针失败也返回完整键集，queue_depth 明确置 None 而不是缺键。
    assert body["queue_depth"] is None
    assert body["embed_model"] == "bge-m3"
    assert HEALTH_KEYS <= set(body)


def test_health_returns_503_when_stats_raises(api, monkeypatch):
    make, _ = api
    client = make("wang")
    from app import server

    class _Service:
        def stats(self):
            raise RuntimeError("db gone")

    monkeypatch.setattr(server, "_service", _Service())
    monkeypatch.setattr(server, "_service_error", None)
    resp = client.get("/api/health")

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["db"].startswith("error:")
    assert HEALTH_KEYS <= set(body)
