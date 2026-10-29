from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from ingest import repository as repo
from ingest.config import get_settings
from tests.ingest.builders import build_xlsx
from tests.ingest.conftest import fetch_all


def upload(client, path, **form):
    return client.post(
        "/api/documents",
        files={"file": (path.name, path.read_bytes())},
        data={k: str(v) for k, v in form.items()},
    )


def test_upload_creates_document_version_and_parse_job(api, tmp_path):
    make, db_url = api
    client = make("wang")
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])

    resp = upload(client, xlsx, title="2025预算", classification=1,
                  effective_date="2026-01-01")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "uploaded" and body["job_id"] > 0

    docs = fetch_all(db_url, "SELECT * FROM documents")
    assert docs[0]["title"] == "2025预算"
    assert docs[0]["dept"] == "财务处"
    assert docs[0]["classification"] == 1
    assert str(docs[0]["effective_date"]) == "2026-01-01"

    versions = fetch_all(db_url, "SELECT * FROM doc_versions")
    assert versions[0]["status"] == "uploaded"
    assert versions[0]["parser"] == "xlsx"
    assert len(versions[0]["sha256"]) == 64
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        stored = conn.execute("SELECT storage_path FROM doc_versions").fetchone()["storage_path"]
    assert (tmp_path / "uploads" / stored).exists()

    jobs = fetch_all(db_url, "SELECT * FROM jobs")
    assert jobs[0]["kind"] == "parse"
    assert jobs[0]["payload"]["version_id"] == str(versions[0]["id"])


def test_rejects_unsupported_type_and_oversize(api, tmp_path, monkeypatch):
    make, db_url = api
    client = make("wang")
    bad = client.post("/api/documents", files={"file": ("a.exe", b"MZ")})
    assert bad.status_code == 400
    assert bad.json()["code"] == "unsupported_type"

    monkeypatch.setenv("INGEST_MAX_FILE_MB", "1")
    get_settings.cache_clear()
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10)
    large = client.post("/api/documents", files={"file": ("a.pdf", big)})
    assert large.status_code == 413
    assert large.json()["code"] == "too_large"
    assert fetch_all(db_url, "SELECT count(*) AS n FROM documents")[0]["n"] == 0


def test_queue_full_returns_429(api, tmp_path, monkeypatch):
    make, _ = api
    client = make("wang")
    monkeypatch.setenv("INGEST_QUEUE_MAX", "0")
    get_settings.cache_clear()
    xlsx = build_xlsx(tmp_path / "a.xlsx", [["部门", "金额"], ["综合处", 1]])
    resp = upload(client, xlsx)
    assert resp.status_code == 429
    assert resp.json()["code"] == "queue_full"


def test_duplicate_content_and_new_version(api, tmp_path):
    make, db_url = api
    client = make("wang")
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    first = upload(client, xlsx)
    assert first.status_code == 202
    doc_id = first.json()["document_id"]

    dup = upload(client, xlsx, document_id=doc_id)
    assert dup.status_code == 409
    assert dup.json()["code"] == "duplicate_content"

    updated = build_xlsx(tmp_path / "预算v2.xlsx", [["部门", "金额"], ["综合处", 20]])
    second = upload(client, updated, document_id=doc_id)
    assert second.status_code == 202
    assert second.json()["document_id"] == doc_id
    versions = fetch_all(db_url, "SELECT version_no FROM doc_versions ORDER BY version_no")
    assert [v["version_no"] for v in versions] == [1, 2]


def test_department_and_classification_permissions(api, tmp_path):
    make, _ = api
    client = make("wang")
    zhang = make("zhang")
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])

    too_secret = upload(zhang, xlsx, classification=2)
    assert too_secret.status_code == 403

    other_dept = upload(zhang, xlsx, dept="人事处")
    assert other_dept.status_code == 403


def test_list_and_detail_hide_other_departments(api, tmp_path, monkeypatch):
    from dataclasses import replace

    from app.users import DEMO_USERS

    # 列表的可见性是「检索 ACL + 本部门待定级」；管理员看全部。
    # 这里临时摘掉 wang 的 admin，验证普通上传者的过滤。
    monkeypatch.setitem(DEMO_USERS, "wang",
                        replace(DEMO_USERS["wang"], roles=("uploader", "reviewer")))
    make, db_url = api
    client = make("wang")
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    upload(client, xlsx, title="2025预算", classification=1)

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        repo.create_document(conn, title="人事名册", dept="人事处", classification=1,
                             effective_date=None, created_by="li")
        conn.commit()

    listing = client.get("/api/documents").json()
    assert listing["total"] == 1
    assert [item["title"] for item in listing["items"]] == ["2025预算"]

    other_id = fetch_all(db_url, "SELECT id FROM documents WHERE dept='人事处'")[0]["id"]
    assert client.get(f"/api/documents/{other_id}").status_code == 404

    detail = client.get(f"/api/documents/{fetch_all(db_url, 'SELECT id FROM documents WHERE dept=%s', ('财务处',))[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["versions"][0]["version_no"] == 1


def test_deactivate_and_soft_delete(api, tmp_path):
    make, db_url = api
    client = make("wang")
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    body = upload(client, xlsx, classification=1).json()
    doc_id, version_id = body["document_id"], body["version_id"]

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        conn.execute(
            """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type, content,
                   content_tokens, embed_model, dept, classification, chunk_hash, active)
               VALUES (%s, %s, 1, 'text', '内容', '内容', 'bge-m3', '财务处', 1, %s, true)""",
            (doc_id, version_id, "a" * 64),
        )
        conn.execute("UPDATE documents SET status='active', current_version_id=%s WHERE id=%s",
                     (version_id, doc_id))
        conn.commit()

    resp = client.post(f"/api/documents/{doc_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"
    assert fetch_all(db_url, "SELECT active FROM chunks")[0]["active"] is False
    assert fetch_all(db_url, "SELECT status FROM documents")[0]["status"] == "inactive"

    deleted = client.delete(f"/api/documents/{doc_id}")
    assert deleted.status_code == 200
    row = fetch_all(db_url, "SELECT status, deleted_at FROM documents")[0]
    assert row["status"] == "deleting" and row["deleted_at"] is not None
    purge = fetch_all(db_url, "SELECT * FROM jobs WHERE kind='purge'")
    assert len(purge) == 1
    assert purge[0]["payload"]["document_id"] == doc_id


def test_delete_requires_admin(api, tmp_path):
    make, db_url = api
    client = make("wang")
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    doc_id = upload(client, xlsx).json()["document_id"]

    # 临时把 wang 的 admin 角色摘掉，验证越权被拒
    from app.users import DEMO_USERS
    original = DEMO_USERS["wang"]
    DEMO_USERS["wang"] = type(original)(
        username=original.username, display_name=original.display_name,
        dept=original.dept, max_level=original.max_level, role=original.role,
        roles=("uploader", "reviewer"))
    try:
        assert client.delete(f"/api/documents/{doc_id}").status_code == 403
    finally:
        DEMO_USERS["wang"] = original
