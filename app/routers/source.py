"""引用回源：按 chunk_id 取切片原文与定位信息。

回源直接展示正文，因此不复用管理台「管理员全量可见」的口径，严格走检索同款
can_view：管理员也不能越过部门/密级限制。所有不可见情形统一 404，
不区分「不存在」与「无权查看」，避免成为枚举文档归属的旁路。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from psycopg import Connection

from app.deps import current_user, get_db
from app.users import User, can_view
from ingest import repository as repo

router = APIRouter(prefix="/api/chunks", tags=["source"])

CHUNK_ID = re.compile(r"^(?P<doc>[0-9a-f-]{36})@v(?P<ver>\d+)#(?P<ord>\d+)$")
_LOCATOR_FIELDS = ("chunk_type", "sheet", "cell_range", "row_range",
                   "page", "bbox", "heading_path", "block_id")

_NOT_FOUND = "切片不存在"


@router.get("/{chunk_id}/source")
def chunk_source(chunk_id: str, user: User = Depends(current_user),
                 conn: Connection = Depends(get_db)) -> dict:
    match = CHUNK_ID.match(chunk_id)
    if match is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    doc = repo.get_document(conn, match["doc"])
    if doc is None or doc["deleted_at"] is not None or doc["status"] != "active":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if not can_view(user, {"classification_level": doc["classification"],
                           "dept": doc["dept"]}):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    version = conn.execute(
        "SELECT * FROM doc_versions WHERE document_id=%s AND version_no=%s",
        (doc["id"], int(match["ver"])),
    ).fetchone()
    if version is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    chunk = conn.execute(
        "SELECT * FROM chunks WHERE version_id=%s AND ordinal=%s",
        (version["id"], int(match["ord"])),
    ).fetchone()
    if chunk is None or not chunk["active"]:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    metadata = chunk["metadata"] or {}
    locator = {key: metadata[key] for key in _LOCATOR_FIELDS
               if metadata.get(key) is not None}
    return {"chunk_id": chunk_id, "document_id": doc["id"],
            "version_id": version["id"], "filename": version["filename"],
            "content": chunk["content"], "locator": locator}
