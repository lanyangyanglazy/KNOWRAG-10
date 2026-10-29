from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg import Connection
from pydantic import BaseModel, Field

from app.deps import audit_event, current_user, get_db
from app.users import User
from ingest.config import get_settings
from ingest.errors import NotFound
from ingest.review_service import (
    approve, confirm, load_version_for_review, summary, waive,
)
from ingest.storage import FileStore

router = APIRouter(prefix="/api/versions", tags=["review"])


class ColumnPatch(BaseModel):
    key: str | None = None
    name: str
    dtype: Literal["string", "number", "date", "bool"] = "string"
    unit: str | None = None


class TablePatch(BaseModel):
    table_key: str
    header_rows: list[int]
    columns: list[ColumnPatch]
    footer_rows: list[int] = Field(default_factory=list)


class MetaPatch(BaseModel):
    title: str | None = None
    dept: str | None = None
    classification: int | None = Field(None, ge=0, le=2)
    effective_date: date | None = None


class ConfirmPayload(BaseModel):
    tables: list[TablePatch] = Field(default_factory=list)
    meta: MetaPatch = Field(default_factory=MetaPatch)
    apply_template: bool = False


class ApprovePayload(BaseModel):
    decision: Literal["approve"] = "approve"


class WaivePayload(BaseModel):
    reason: str = ""
    review_item_id: int | None = None


@router.get("/{version_id}/review")
def review_summary(version_id: str, user: User = Depends(current_user),
                   conn: Connection = Depends(get_db)) -> dict:
    return summary(conn, version_id, user)


@router.get("/{version_id}/preview")
def preview(version_id: str, page: int | None = Query(None, ge=1),
            user: User = Depends(current_user),
            conn: Connection = Depends(get_db)):
    version, _ = load_version_for_review(conn, version_id, user)
    if version["parser"] == "pdf" and page:
        import pymupdf as fitz

        settings = get_settings()
        path = FileStore(settings.upload_dir).resolve(version["storage_path"])
        if not path.exists():
            raise NotFound("原件不存在")
        with fitz.open(path) as pdf:
            if page > len(pdf):
                raise HTTPException(status_code=404, detail="页码超出范围")
            pix = pdf[page - 1].get_pixmap(dpi=120)
            return Response(content=pix.tobytes("png"), media_type="image/png")
    data = summary(conn, version_id, user)
    return {"kind": version["parser"], "text": data["text_preview"],
            "tables": [{"table_key": t["table_key"], "sheet": t["sheet"],
                        "range": t["range"], "grid": t["grid"]}
                       for t in data["tables"]]}


@router.post("/{version_id}/confirm")
def confirm_version(version_id: str, payload: ConfirmPayload, request: Request,
                    user: User = Depends(current_user),
                    conn: Connection = Depends(get_db)) -> dict:
    result = confirm(conn, version_id, payload, user=user)
    audit_event(request, user, "review_submit", version_id=version_id,
                doc_id=result["document_id"],
                classification=result["classification"],
                detail={"status": result["status"],
                        "pending_approvals": result.get("pending_approvals", 0)})
    # 与 confirm() 的保存条件一致：没有表格补丁时不会保存模板，不能写假的审计事件；
    # 实际只为 tables[0] 存模板，审计也只记录这一张表的 key。
    if payload.apply_template and payload.tables:
        audit_event(request, user, "template_apply", version_id=version_id,
                    doc_id=result["document_id"],
                    classification=result["classification"],
                    detail={"table_keys": [payload.tables[0].table_key]})
    return result


@router.post("/{version_id}/approve")
def approve_version(version_id: str, payload: ApprovePayload, request: Request,
                    user: User = Depends(current_user),
                    conn: Connection = Depends(get_db)) -> dict:
    result = approve(conn, version_id, user=user)
    audit_event(request, user, "review_approve", version_id=version_id,
                doc_id=result["document_id"],
                classification=result["classification"],
                detail={"status": result["status"],
                        "pending_approvals": result.get("pending_approvals", 0)})
    return result


@router.post("/{version_id}/waive")
def waive_version(version_id: str, payload: WaivePayload, request: Request,
                  user: User = Depends(current_user),
                  conn: Connection = Depends(get_db)) -> dict:
    if not payload.reason.strip():
        raise HTTPException(status_code=400, detail="waive 必须填写理由")
    result = waive(conn, version_id, reason=payload.reason,
                   item_id=payload.review_item_id, user=user)
    audit_event(request, user, "review_waive", version_id=version_id,
                doc_id=result["document_id"],
                classification=result["classification"],
                detail={"reason": payload.reason, "item_id": payload.review_item_id})
    return result
