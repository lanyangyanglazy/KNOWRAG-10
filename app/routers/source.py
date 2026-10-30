"""引用回源：按 chunk_id 取切片原文与定位信息。

回源直接展示正文，因此不复用管理台「管理员全量可见」的口径，严格走检索同款
can_view：管理员也不能越过部门/密级限制。所有不可见情形统一 404，
不区分「不存在」与「无权查看」，避免成为枚举文档归属的旁路。
"""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg import Connection

from app.deps import current_user, get_db
from app.users import User, can_view
from ingest import repository as repo

router = APIRouter(prefix="/api/chunks", tags=["source"])

# 尾部的版本/序号限制位数：不限长时 int() 在超长数字上会抛 ValueError（→ 500）。
CHUNK_ID = re.compile(r"^(?P<doc>[0-9a-f-]{36})@v(?P<ver>\d{1,9})#(?P<ord>\d{1,9})$")
_LOCATOR_FIELDS = ("chunk_type", "sheet", "cell_range", "row_range",
                   "page", "bbox", "heading_path", "block_id")

_NOT_FOUND = "切片不存在"


def _parse_doc_id(raw: str) -> UUID:
    """正则只约束长度与字符集（36 个连字符也能通过），必须真正解析 UUID。"""
    try:
        return UUID(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc


@router.get("/{chunk_id}/source")
def chunk_source(chunk_id: str, user: User = Depends(current_user),
                 conn: Connection = Depends(get_db)) -> dict:
    match = CHUNK_ID.match(chunk_id)
    if match is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    # 先解析再进 SQL：非法 UUID 直接作为 404 处理，不能被 psycopg 的
    # InvalidTextRepresentation 冒泡成 500。
    doc_id = _parse_doc_id(match["doc"])
    doc = repo.get_document(conn, doc_id)
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
