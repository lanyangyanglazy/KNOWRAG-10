from __future__ import annotations

from psycopg import Connection

from ingest import repository as repo
from ingest.errors import DocumentInactive, NotFound
from ingest.queue import enqueue


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
