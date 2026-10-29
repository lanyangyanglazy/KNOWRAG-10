from __future__ import annotations

import logging
from datetime import date
from typing import Iterator
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from psycopg import Connection

from app.users import CLASSIFICATION_LEVELS, User

logger = logging.getLogger("rag.api.deps")


def current_user(request: Request) -> User:
    """延迟导入 server，避免 server → routers → deps 的循环导入。"""
    from app import server

    return server.current_user(request)


def get_db() -> Iterator[Connection]:
    from ingest.db import connection

    with connection() as conn:
        yield conn


def require_roles(user: User, *roles: str) -> None:
    if not set(roles) & set(user.roles):
        raise HTTPException(status_code=403, detail="无权限执行该操作")


def parse_classification(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        level = value
    elif str(value).isdigit():
        level = int(value)
    else:
        level = CLASSIFICATION_LEVELS.get(str(value))
    if level not in (0, 1, 2):
        raise HTTPException(status_code=400,
                            detail="classification 只能是 0/1/2 或 公开/内部/秘密")
    return level


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400,
                            detail="effective_date 必须是 YYYY-MM-DD") from exc


def client_ip(request: Request) -> str:
    from app import server

    return server._client_ip(request)


def audit_event(request: Request, user: User, action: str, **fields) -> None:
    """
    写结构化审计。

    必须走 server.write_audit 兼容包装：测试通过 patch server.AUDIT_FILE 指定落盘位置，
    直接调 app.audit 会绕过这个 patch 点、写进仓库 logs/。DB 行已是 durable 证据，
    审计写盘失败只记系统日志，不能把已提交成功的上传变成 500 诱发客户端重传。
    """
    from app import server

    record = {
        "request_id": request.headers.get("x-request-id") or uuid4().hex,
        "action": action,
        "user": user.username,
        "dept": user.dept,
        "role": user.role,
        "ip": client_ip(request),
    }
    for key, value in fields.items():
        record[key] = str(value) if isinstance(value, UUID) else value
    try:
        server.write_audit(record)
    except OSError as exc:
        logger.error("audit.write_failed action=%s err=%s", action, exc)
