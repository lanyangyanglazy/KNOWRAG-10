from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from psycopg import Connection

from app.deps import (
    audit_event, current_user, get_db, parse_classification, parse_date, require_roles,
)
from app.users import User, can_view
from ingest import repository as repo
from ingest.activation import deactivate_document, soft_delete_document
from ingest.upload_service import save_upload

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _visible(user: User, doc: dict) -> bool:
    """管理台可见性 = 检索 ACL + 本部门待定级（否则复核台打不开）。"""
    if "admin" in user.roles:
        return True
    if doc["dept"] == user.dept and doc["classification"] is None:
        return True
    return can_view(user, {
        "classification_level": doc["classification"],
        "dept": doc["dept"],
    })


def _can_manage(user: User, doc: dict) -> bool:
    if "admin" in user.roles:
        return True
    if not {"uploader", "reviewer"} & set(user.roles):
        return False
    return doc["dept"] == user.dept


def _store(request: Request, user: User, conn: Connection, file: UploadFile,
           *, title: str | None, dept: str | None, classification: str | None,
           effective_date: str | None, document_id: str | None) -> dict:
    require_roles(user, "uploader", "admin")
    target_dept = dept or user.dept
    if target_dept != user.dept and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="只能上传到本部门")
    level = parse_classification(classification)
    if level is not None and level > user.max_level:
        raise HTTPException(status_code=403, detail="密级超过你的权限")
    result = save_upload(
        conn, file.file, file.filename or "", file.content_type,
        created_by=user.username, dept=target_dept, classification=level,
        effective_date=parse_date(effective_date), title=title,
        document_id=document_id,
    )
    audit_event(request, user, "upload", doc_id=result["document_id"],
                version_id=result["version_id"], classification=level,
                detail={"filename": file.filename, "size_bytes": file.size})
    return result


@router.post("", status_code=202)
def upload_document(request: Request, file: UploadFile = File(...),
                    title: str | None = Form(None), dept: str | None = Form(None),
                    classification: str | None = Form(None),
                    effective_date: str | None = Form(None),
                    document_id: str | None = Form(None),
                    user: User = Depends(current_user),
                    conn: Connection = Depends(get_db)) -> dict:
    return _store(request, user, conn, file, title=title, dept=dept,
                  classification=classification, effective_date=effective_date,
                  document_id=document_id)


@router.post("/{document_id}/versions", status_code=202)
def upload_version(document_id: str, request: Request, file: UploadFile = File(...),
                   title: str | None = Form(None), dept: str | None = Form(None),
                   classification: str | None = Form(None),
                   effective_date: str | None = Form(None),
                   user: User = Depends(current_user),
                   conn: Connection = Depends(get_db)) -> dict:
    return _store(request, user, conn, file, title=title, dept=dept,
                  classification=classification, effective_date=effective_date,
                  document_id=document_id)


@router.get("")
def list_documents(status: str | None = None, dept: str | None = None,
                   q: str | None = None, page: int = 1, page_size: int = 20,
                   user: User = Depends(current_user),
                   conn: Connection = Depends(get_db)) -> dict:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where = ["d.deleted_at IS NULL"]
    params: list[Any] = []
    if "admin" not in user.roles:
        where.append("((d.classification IS NOT NULL AND d.classification <= %s "
                     "AND (d.classification = 0 OR d.dept = %s)) "
                     "OR (d.dept = %s AND d.classification IS NULL))")
        params += [user.max_level, user.dept, user.dept]
    if dept:
        where.append("d.dept = %s")
        params.append(dept)
    if q:
        where.append("d.title ILIKE %s")
        params.append(f"%{q}%")
    if status:
        where.append("v.status = %s")
        params.append(status)
    cond = " AND ".join(where)
    base = f"""FROM documents d
               LEFT JOIN LATERAL (
                   SELECT status, version_no FROM doc_versions v
                   WHERE v.document_id = d.id ORDER BY version_no DESC LIMIT 1
               ) v ON true
               WHERE {cond}"""
    total = conn.execute(f"SELECT count(*) AS n {base}", params).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT d.id, d.title, d.dept, d.classification, d.status,
                   d.effective_date, d.created_at, d.updated_at,
                   v.status AS version_status, v.version_no
            {base}
            ORDER BY d.updated_at DESC, d.id
            LIMIT %s OFFSET %s""",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {"total": int(total), "page": page, "page_size": page_size,
            "items": [dict(row) for row in rows]}


@router.get("/{document_id}")
def get_document(document_id: str, user: User = Depends(current_user),
                 conn: Connection = Depends(get_db)) -> dict:
    doc = repo.get_document(conn, document_id)
    if doc is None or doc["deleted_at"] is not None or not _visible(user, doc):
        raise HTTPException(status_code=404, detail="文档不存在")
    open_reviews = conn.execute(
        """SELECT count(*) AS n FROM review_items r
           JOIN doc_versions v ON v.id = r.version_id
           WHERE v.document_id=%s AND r.status='open'""",
        (document_id,),
    ).fetchone()["n"]
    return {"document": doc, "versions": repo.list_versions(conn, document_id),
            "open_reviews": int(open_reviews)}


@router.post("/{document_id}/deactivate")
def deactivate(document_id: str, request: Request,
               user: User = Depends(current_user),
               conn: Connection = Depends(get_db)) -> dict:
    doc = repo.get_document(conn, document_id)
    if doc is None or doc["deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if not _can_manage(user, doc):
        raise HTTPException(status_code=403, detail="无权下线该文档")
    result = deactivate_document(conn, document_id, actor=user.username)
    audit_event(request, user, "deactivate", doc_id=document_id,
                classification=doc["classification"])
    return result


@router.delete("/{document_id}")
def delete_document(document_id: str, request: Request,
                    user: User = Depends(current_user),
                    conn: Connection = Depends(get_db)) -> dict:
    require_roles(user, "admin")
    doc = repo.get_document(conn, document_id)
    if doc is None or doc["deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="文档不存在")
    result = soft_delete_document(conn, document_id, actor=user.username)
    audit_event(request, user, "delete", doc_id=document_id,
                classification=doc["classification"])
    return result
