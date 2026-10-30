from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import BinaryIO

from psycopg import Connection

from ingest import repository as repo
from ingest.config import Settings, get_settings
from ingest.errors import (
    DocumentInactive, DuplicateContent, Forbidden, NotFound, QueueFull, TooLarge,
    UnsupportedType,
)
from ingest.queue import enqueue, queue_depth
from ingest.storage import FileStore

SUPPORTED = {
    ".docx": ("docx", "0.1.0"),
    ".doc": ("doc", "0.1.0"),
    ".xlsx": ("xlsx", "0.1.0"),
    ".xlsm": ("xlsx", "0.1.0"),
    ".pdf": ("pdf", "0.1.0"),
}
READ_SIZE = 1024 * 1024


def save_upload(conn: Connection, src: BinaryIO, filename: str,
                content_type: str | None, *, created_by: str, dept: str,
                classification: int | None, effective_date: date | None = None,
                title: str | None = None, document_id: str | None = None,
                max_level: int | None = None,
                settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise UnsupportedType(f"不支持的文件类型: {suffix or '无扩展名'}")
    if queue_depth(conn) >= settings.queue_max:
        raise QueueFull("解析队列已满，请稍后重试")
    parser, parser_version = SUPPORTED[suffix]

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.upload_dir / f".upload-{os.getpid()}-{id(src)}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = src.read(READ_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise TooLarge(f"文件超过 {settings.max_file_mb} MB")
                digest.update(chunk)
                out.write(chunk)
        sha256 = digest.hexdigest()
        storage_path = FileStore(settings.upload_dir).put_file(tmp, sha256)
    finally:
        tmp.unlink(missing_ok=True)

    if document_id:
        doc = repo.lock_document(conn, document_id)
        if doc is None:
            raise NotFound("文档不存在")
        if doc["deleted_at"] is not None or doc["status"] == "deleting":
            raise DocumentInactive("文档已删除，不能追加版本")
        if doc["dept"] != dept:
            raise Forbidden("只能为本部门文档追加版本")
        if (max_level is not None and doc["classification"] is not None
                and doc["classification"] > max_level):
            raise Forbidden("无权访问该文档")
        # 密级只能「未定级 → 具体值」补齐，任何已有密级的变更（含 admin）都必须走复核。
        if (classification is not None and doc["classification"] is not None
                and classification != doc["classification"]):
            raise Forbidden("密级变更需走复核流程")
        updates: dict = {}
        if classification is not None:
            updates["classification"] = classification
        if effective_date is not None:
            updates["effective_date"] = effective_date
        if title:
            updates["title"] = title
        if updates:
            repo.set_document_meta(conn, document_id, **updates)
    else:
        doc = repo.create_document(
            conn, title=title or Path(filename).stem, dept=dept,
            classification=classification, effective_date=effective_date,
            created_by=created_by,
        )
        document_id = doc["id"]

    duplicate = conn.execute(
        "SELECT id FROM doc_versions WHERE document_id=%s AND sha256=%s",
        (document_id, sha256),
    ).fetchone()
    if duplicate:
        conn.rollback()
        raise DuplicateContent("相同内容已上传")

    version = repo.create_version(
        conn, document_id=document_id,
        version_no=repo.next_version_no(conn, document_id),
        sha256=sha256, filename=Path(filename).name,
        mime=content_type or "application/octet-stream", size_bytes=size,
        storage_path=storage_path, parser=parser, parser_version=parser_version,
    )
    # psycopg 的 Jsonb 默认 dumps 不认识 UUID，payload 里必须先转字符串。
    job_id = enqueue(conn, "parse", {"version_id": str(version["id"])}, priority=50)
    return {"document_id": document_id, "version_id": version["id"],
            "job_id": job_id, "status": "uploaded"}
