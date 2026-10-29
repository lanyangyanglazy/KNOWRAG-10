from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from ingest import activation, pipeline, repository as repo
from ingest.config import get_settings
from ingest.errors import DocumentInactive, ReviewRequired
from ingest.queue import claim, enqueue
from ingest.storage import FileStore
from ingest.worker import run_job


@pytest.fixture()
def audited(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(activation, "write_audit", records.append)
    monkeypatch.setattr(pipeline, "write_audit", records.append)
    return records


def _seed(conn, *, classification=1, status="draft"):
    doc = repo.create_document(
        conn, title="预算文档", dept="财务处", classification=classification,
        effective_date=None, created_by="zhang")
    if status != "draft":
        conn.execute("UPDATE documents SET status=%s WHERE id=%s",
                     (status, doc["id"]))
    conn.commit()
    return repo.get_document(conn, doc["id"])


def _version(conn, doc, no, *, status="ready", storage_path=None):
    return repo.create_version(
        conn, document_id=doc["id"], version_no=no, sha256=str(no) * 64,
        filename=f"v{no}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=100, storage_path=storage_path or f"aa/{no}",
        parser="docx", parser_version="0.1.0", status=status)


def _chunks(conn, doc, version, *, count=2, active=False):
    for i in range(count):
        conn.execute(
            """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type,
                   content, content_tokens, embed_model, dept, classification,
                   metadata, chunk_hash, active)
               VALUES (%s,%s,%s,'text',%s,%s,'bge-m3',%s,%s,'{}',%s,%s)""",
            (doc["id"], version["id"], i, f"正文{i}", f"正文{i}", doc["dept"],
             doc["classification"], f"{version['id']}-{i}".ljust(64, "0"), active))
    conn.commit()


def test_activate_first_version_makes_document_active(conn, audited):
    doc = _seed(conn)
    version = _version(conn, doc, 1)
    _chunks(conn, doc, version)

    result = activation.activate_version(conn, version["id"])

    assert result["activated"] is True
    assert result["previous_version_id"] is None
    row = repo.get_document(conn, doc["id"])
    assert row["status"] == "active"
    assert str(row["current_version_id"]) == str(version["id"])
    flags = conn.execute("SELECT active FROM chunks WHERE version_id=%s",
                         (version["id"],)).fetchall()
    assert len(flags) == 2 and all(f["active"] for f in flags)
    assert conn.execute("SELECT activated_at FROM doc_versions WHERE id=%s",
                        (version["id"],)).fetchone()["activated_at"] is not None
    assert [r["action"] for r in audited] == ["activate"]


def test_activate_switches_version_and_deactivates_old(conn, audited):
    doc = _seed(conn)
    first = _version(conn, doc, 1)
    _chunks(conn, doc, first)
    activation.activate_version(conn, first["id"])

    second = _version(conn, doc, 2)
    _chunks(conn, doc, second)
    result = activation.activate_version(conn, second["id"])

    assert result["previous_version_id"] == str(first["id"])
    row = repo.get_document(conn, doc["id"])
    assert row["status"] == "active"
    assert str(row["current_version_id"]) == str(second["id"])
    old = conn.execute(
        "SELECT bool_or(active) AS any_active FROM chunks WHERE version_id=%s",
        (first["id"],)).fetchone()
    new = conn.execute(
        "SELECT bool_and(active) AS all_active FROM chunks WHERE version_id=%s",
        (second["id"],)).fetchone()
    assert old["any_active"] is False
    assert new["all_active"] is True


def test_activate_refuses_unclassified_document(conn, audited):
    doc = _seed(conn, classification=None)
    version = _version(conn, doc, 1)
    _chunks(conn, doc, version)

    with pytest.raises(ReviewRequired):
        activation.activate_version(conn, version["id"])

    assert repo.get_document(conn, doc["id"])["status"] == "draft"
    assert conn.execute(
        "SELECT count(*) AS n FROM chunks WHERE active").fetchone()["n"] == 0
    assert audited == []


def test_activate_requires_ready_version(conn, audited):
    doc = _seed(conn)
    version = _version(conn, doc, 1, status="embedding")
    _chunks(conn, doc, version)

    with pytest.raises(ReviewRequired):
        activation.activate_version(conn, version["id"])


def test_activate_requires_chunks(conn, audited):
    doc = _seed(conn)
    version = _version(conn, doc, 1)

    with pytest.raises(ReviewRequired):
        activation.activate_version(conn, version["id"])


def test_inactive_document_is_not_revived(conn, audited):
    doc = _seed(conn)
    first = _version(conn, doc, 1)
    _chunks(conn, doc, first)
    activation.activate_version(conn, first["id"])
    activation.deactivate_document(conn, doc["id"], actor="admin")

    second = _version(conn, doc, 2)
    _chunks(conn, doc, second)
    result = activation.activate_version(conn, second["id"])

    assert result["activated"] is False
    assert result["reason"] == "document_inactive"
    row = repo.get_document(conn, doc["id"])
    assert row["status"] == "inactive"
    # Task 14 的下线会清空 current_version_id；激活不得把旧版本重新指回来。
    assert row["current_version_id"] is None
    assert conn.execute(
        "SELECT count(*) AS n FROM chunks WHERE active").fetchone()["n"] == 0


def test_activate_is_idempotent(conn, audited):
    doc = _seed(conn)
    version = _version(conn, doc, 1)
    _chunks(conn, doc, version)

    activation.activate_version(conn, version["id"])
    again = activation.activate_version(conn, version["id"])

    assert again["activated"] is False
    assert again["reason"] == "already_active"
    assert len(audited) == 1


def test_activation_prunes_chunks_beyond_retention(conn, audited):
    settings = replace(get_settings(), keep_versions=2, keep_days=180)
    doc = _seed(conn)
    versions = []
    for no in (1, 2, 3, 4):
        version = _version(conn, doc, no)
        _chunks(conn, doc, version)
        versions.append(version)

    result = activation.activate_version(conn, versions[-1]["id"],
                                         settings=settings)

    counts = {
        no: conn.execute("SELECT count(*) AS n FROM chunks WHERE version_id=%s",
                         (version["id"],)).fetchone()["n"]
        for no, version in zip((1, 2, 3, 4), versions)
    }
    assert counts == {1: 0, 2: 0, 3: 2, 4: 2}
    assert result["pruned_chunks"] == 4
    assert conn.execute(
        "SELECT count(*) AS n FROM doc_versions WHERE document_id=%s",
        (doc["id"],)).fetchone()["n"] == 4


def test_purge_removes_rows_and_file_but_keeps_tombstone(conn, audited, tmp_path):
    settings = replace(get_settings(), upload_dir=tmp_path / "uploads")
    doc = _seed(conn)
    source = tmp_path / "a.docx"
    source.write_bytes(b"hello")
    store = FileStore(settings.upload_dir)
    rel = store.put_file(source, hashlib.sha256(b"hello").hexdigest())
    version = _version(conn, doc, 1, storage_path=rel)
    _chunks(conn, doc, version)
    conn.execute(
        """INSERT INTO doc_tables (version_id, table_key, header_rows, columns,
               row_count, confidence, source)
           VALUES (%s, 't1', '{1}', '[]'::jsonb, 0, 0.9, 'heuristic')""",
        (version["id"],))
    item_id = repo.ensure_review_item(conn, version["id"], "structure",
                                      {"reason": "structure"})
    repo.add_approval(conn, item_id, "li")
    conn.commit()

    # documents.id 是 uuid 对象；jobs.payload 是 jsonb，入队需字符串（与接口层一致）。
    activation.soft_delete_document(conn, str(doc["id"]), actor="admin")
    result = activation.purge_document(conn, doc["id"], actor="admin",
                                       settings=settings)

    assert result["versions"] == 1
    assert result["files_removed"] == 1
    assert not store.resolve(rel).exists()
    counts = conn.execute(
        """SELECT (SELECT count(*) FROM chunks) AS chunks,
                  (SELECT count(*) FROM doc_tables) AS doc_tables,
                  (SELECT count(*) FROM doc_versions) AS versions,
                  (SELECT count(*) FROM review_items) AS items,
                  (SELECT count(*) FROM review_approvals) AS approvals"""
    ).fetchone()
    assert dict(counts) == {"chunks": 0, "doc_tables": 0, "versions": 0,
                            "items": 0, "approvals": 0}
    tomb = repo.get_document(conn, doc["id"])
    assert tomb["status"] == "deleting"
    assert tomb["deleted_at"] is not None
    assert tomb["current_version_id"] is None
    assert audited[-1]["action"] == "delete"


def test_purge_keeps_shared_blob_until_last_reference(conn, audited, tmp_path):
    settings = replace(get_settings(), upload_dir=tmp_path / "uploads")
    source = tmp_path / "shared.docx"
    source.write_bytes(b"shared")
    store = FileStore(settings.upload_dir)
    rel = store.put_file(source, hashlib.sha256(b"shared").hexdigest())

    docs = []
    for _ in range(2):
        doc = _seed(conn)
        _version(conn, doc, 1, storage_path=rel)
        conn.execute(
            "UPDATE documents SET status='deleting', deleted_at=now() WHERE id=%s",
            (doc["id"],))
        conn.commit()
        docs.append(doc)

    activation.purge_document(conn, docs[0]["id"], settings=settings)
    assert store.resolve(rel).exists()

    activation.purge_document(conn, docs[1]["id"], settings=settings)
    assert not store.resolve(rel).exists()


def test_purge_refuses_live_document(conn, audited, tmp_path):
    settings = replace(get_settings(), upload_dir=tmp_path / "uploads")
    doc = _seed(conn)

    with pytest.raises(DocumentInactive):
        activation.purge_document(conn, doc["id"], settings=settings)


def test_embed_failure_dead_letters_to_review(conn, audited, monkeypatch):
    doc = _seed(conn)
    version = _version(conn, doc, 1)

    def boom(conn, version_id, **kwargs):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(pipeline, "embed_version", boom)
    enqueue(conn, "embed", {"version_id": str(version["id"])}, max_attempts=0)
    job = claim(conn, "w")
    assert run_job(conn, job, pipeline.HANDLERS, "w",
                   pipeline.DEAD_LETTER_HOOKS) is False

    row = conn.execute("SELECT status, error FROM doc_versions WHERE id=%s",
                       (version["id"],)).fetchone()
    assert row["status"] == "failed"
    assert "ollama down" in row["error"]
    items = repo.list_open_review_items(conn, version["id"])
    assert [i["kind"] for i in items] == ["parse_warning"]
    assert audited[-1]["action"] == "embed_failed"


def test_pipeline_registers_activation_handlers(conn, audited):
    assert pipeline.HANDLERS["activate"] is pipeline.handle_activate
    assert pipeline.HANDLERS["purge"] is pipeline.handle_purge
    assert set(pipeline.DEAD_LETTER_HOOKS) == {"parse", "embed", "activate", "purge"}

    # 文档已被清理（版本行不存在）时，死信钩子不得插入复核项：外键失败会
    # 逃出 run_job 直接杀死 worker 循环；只允许尽力写审计。
    job = {"id": 999999,
           "payload": {"version_id": "00000000-0000-0000-0000-000000000000"}}
    pipeline.handle_activate_dead_letter(conn, job)
    pipeline.handle_embed_dead_letter(conn, job)

    assert [r["action"] for r in audited] == ["activate_failed", "embed_failed"]
    assert conn.execute(
        "SELECT count(*) AS n FROM review_items").fetchone()["n"] == 0
