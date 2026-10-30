from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from psycopg import Connection
from pydantic import BaseModel

from app.deps import audit_event, current_user, get_db, require_roles
from app.users import User
from ingest import repository as repo
from ingest.review_service import save_template

router = APIRouter(prefix="/api/templates", tags=["templates"])


class TemplatePayload(BaseModel):
    version_id: str
    table_key: str
    name: str
    scope_dept: str | None = None


@router.get("")
def list_templates(user: User = Depends(current_user),
                   conn: Connection = Depends(get_db)) -> dict:
    if "admin" in user.roles:
        rows = conn.execute(
            "SELECT * FROM layout_templates ORDER BY updated_at DESC").fetchall()
    else:
        rows = repo.list_templates(conn, user.dept)
    return {"items": [dict(row) for row in rows]}


@router.post("", status_code=201)
def create_template(payload: TemplatePayload, request: Request,
                    user: User = Depends(current_user),
                    conn: Connection = Depends(get_db)) -> dict:
    require_roles(user, "reviewer", "admin")
    row = save_template(conn, payload.version_id, table_key=payload.table_key,
                        name=payload.name, scope_dept=payload.scope_dept, user=user)
    audit_event(request, user, "template_create", version_id=payload.version_id,
                detail={"table_key": payload.table_key, "name": payload.name,
                        "scope_dept": row["scope_dept"]})
    return row
