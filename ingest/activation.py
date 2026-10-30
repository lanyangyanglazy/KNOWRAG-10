from __future__ import annotations

from psycopg import Connection

from app.audit import write_audit
from ingest import repository as repo
from ingest.config import Settings, get_settings
from ingest.errors import DocumentInactive, NotFound, ReviewRequired
from ingest.queue import enqueue
from ingest.storage import FileStore

# 只有这两种状态允许新版本激活；inactive/deleting 是终态，避免误操作后旧内容自动复活
_ACTIVATABLE_STATUSES = {"draft", "active"}


def deactivate_document(conn: Connection, document_id: str, *, actor: str) -> dict:
    doc = repo.lock_document(conn, document_id)
    if doc is None:
        raise NotFound("文档不存在")
    # 锁内重查：删除流程可能已把状态改为 deleting，不能覆盖回 inactive。
    if doc["deleted_at"] is not None or doc["status"] == "deleting":
        raise DocumentInactive("文档已删除")
    conn.execute("UPDATE chunks SET active=false WHERE document_id=%s", (document_id,))
    conn.execute(
        """UPDATE documents
           SET status='inactive', current_version_id=NULL, updated_at=now()
           WHERE id=%s""",
        (document_id,),
    )
    conn.commit()
    return {"document_id": document_id, "status": "inactive"}


def soft_delete_document(conn: Connection, document_id: str, *, actor: str) -> dict:
    doc = repo.lock_document(conn, document_id)
    if doc is None:
        raise NotFound("文档不存在")
    if doc["status"] == "deleting":
        return {"document_id": document_id, "status": "deleting", "queued": False}
    conn.execute("UPDATE chunks SET active=false WHERE document_id=%s", (document_id,))
    conn.execute(
        """UPDATE documents SET status='deleting', deleted_at=now(), updated_at=now()
           WHERE id=%s""",
        (document_id,),
    )
    enqueue(conn, "purge", {"document_id": document_id}, priority=200)
    return {"document_id": document_id, "status": "deleting", "queued": True}


def _prune_superseded(conn: Connection, document_id, version: dict,
                      settings: Settings) -> int:
    """删除超出保留窗口的旧版本切片；版本行与 doc_tables 保留。"""
    pruned = 0
    if settings.keep_versions > 0:
        cutoff = int(version["version_no"]) - settings.keep_versions
        if cutoff > 0:
            cur = conn.execute(
                """DELETE FROM chunks
                    WHERE document_id=%s AND version_id<>%s
                      AND version_id IN (
                          SELECT id FROM doc_versions
                           WHERE document_id=%s AND version_no<=%s)""",
                (document_id, version["id"], document_id, cutoff),
            )
            pruned += cur.rowcount
    if settings.keep_days > 0:
        cur = conn.execute(
            """DELETE FROM chunks
                WHERE document_id=%s AND version_id<>%s
                  AND version_id IN (
                      SELECT id FROM doc_versions
                       WHERE document_id=%s AND activated_at IS NOT NULL
                         AND activated_at < now() - make_interval(days => %s))""",
            (document_id, version["id"], document_id, settings.keep_days),
        )
        pruned += cur.rowcount
    return pruned


def activate_version(conn: Connection, version_id: str, *, actor: str = "worker",
                     settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    version = repo.lock_version(conn, version_id)
    if version is None:
        raise NotFound("版本不存在")
    doc = repo.lock_document(conn, version["document_id"])
    if doc is None or doc["deleted_at"] is not None:
        raise NotFound("文档不存在")

    # 下线/删除是终态：激活任务只提交空操作，绝不把文档改回 active。
    if doc["status"] not in _ACTIVATABLE_STATUSES:
        conn.commit()
        return {"version_id": str(version["id"]), "activated": False,
                "status": doc["status"], "reason": "document_inactive"}

    if doc["classification"] is None:
        raise ReviewRequired("未定级文档不可激活")

    if (doc["status"] == "active"
            and doc["current_version_id"] == version["id"]):
        conn.commit()
        return {"version_id": str(version["id"]), "activated": False,
                "status": "active", "reason": "already_active"}

    if version["status"] != "ready":
        raise ReviewRequired(f"版本状态为 {version['status']}，不可激活")

    count = conn.execute(
        "SELECT count(*) AS n FROM chunks WHERE version_id=%s",
        (version["id"],),
    ).fetchone()["n"]
    if count == 0:
        raise ReviewRequired("版本没有可激活的切片")

    # 同一个事务内完成切换：旧版本切片下线、新版本切片上线、文档指针与
    # 版本激活时间一起提交，不会出现「新旧都激活」或「都不激活」。
    previous = doc["current_version_id"]
    conn.execute(
        """UPDATE chunks SET active=false
           WHERE document_id=%s AND version_id<>%s AND active""",
        (doc["id"], version["id"]),
    )
    conn.execute("UPDATE chunks SET active=true WHERE version_id=%s",
                 (version["id"],))
    conn.execute(
        """UPDATE documents SET status='active', current_version_id=%s,
               updated_at=now() WHERE id=%s""",
        (version["id"], doc["id"]),
    )
    conn.execute(
        """UPDATE doc_versions SET activated_at=now()
           WHERE id=%s AND activated_at IS NULL""",
        (version["id"],),
    )
    pruned = _prune_superseded(conn, doc["id"], version, settings)
    conn.commit()

    # 审计是证据而非事务参与者：写失败不能让已经生效的激活回滚。
    try:
        write_audit({
            "action": "activate", "user": actor, "dept": doc["dept"],
            "doc_id": str(doc["id"]), "version_id": str(version["id"]),
            "classification": doc["classification"],
            "detail": {"previous_version_id": str(previous) if previous else None,
                       "pruned_chunks": pruned},
        })
    except Exception:  # noqa: BLE001 - 激活已提交，审计失败只影响审计文件
        pass
    return {"version_id": str(version["id"]), "activated": True,
            "status": "active",
            "previous_version_id": str(previous) if previous else None,
            "pruned_chunks": pruned}


def purge_document(conn: Connection, document_id: str, *, actor: str = "worker",
                   settings: Settings | None = None) -> dict:
    """物理清理已软删除的文档；保留 documents 墓碑行。"""
    settings = settings or get_settings()
    doc = repo.lock_document(conn, document_id)
    if doc is None:
        raise NotFound("文档不存在")
    if doc["status"] != "deleting":
        raise DocumentInactive("只有已删除的文档才可清理")

    versions = repo.list_versions(conn, document_id)
    version_ids = [v["id"] for v in versions]
    store = FileStore(settings.upload_dir)

    # 先删原件（引用计数为 0 时），再删数据库行：中途失败重跑可收敛，
    # 反过来会先丢路径、留下永远无法回收的孤儿文件。
    stored = sorted({v["storage_path"] for v in versions})
    removed = 0
    for rel in stored:
        other = conn.execute(
            """SELECT 1 FROM doc_versions
                WHERE storage_path=%s AND document_id<>%s LIMIT 1""",
            (rel, document_id),
        ).fetchone()
        if other is None:
            store.delete(rel)
            removed += 1

    conn.execute("DELETE FROM chunks WHERE document_id=%s", (document_id,))
    if version_ids:
        conn.execute("DELETE FROM doc_tables WHERE version_id = ANY(%s)",
                     (version_ids,))
        conn.execute(
            """DELETE FROM review_approvals WHERE review_item_id IN
               (SELECT id FROM review_items WHERE version_id = ANY(%s))""",
            (version_ids,),
        )
        conn.execute("DELETE FROM review_items WHERE version_id = ANY(%s)",
                     (version_ids,))
    conn.execute("DELETE FROM doc_versions WHERE document_id=%s", (document_id,))
    conn.execute(
        """UPDATE documents SET current_version_id=NULL, status='deleting',
               deleted_at=COALESCE(deleted_at, now()), updated_at=now()
           WHERE id=%s""",
        (document_id,),
    )
    conn.commit()

    # 同上：审计失败不影响已经完成的物理清理。
    try:
        write_audit({
            "action": "delete", "user": actor, "dept": doc["dept"],
            "doc_id": str(doc["id"]), "classification": doc["classification"],
            "detail": {"stage": "purged", "versions": len(versions),
                       "files_removed": removed},
        })
    except Exception:  # noqa: BLE001 - 清理已提交，审计失败只影响审计文件
        pass
    return {"document_id": str(doc["id"]), "status": "deleting",
            "versions": len(versions), "files_removed": removed}
