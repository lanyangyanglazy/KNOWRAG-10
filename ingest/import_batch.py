"""批量导入目录下的支持格式文件（trusted_import，用于内部测试语料）。

与 ``ingest.import_gov_docs`` 的区别：

- 后者只接受带 front matter 的 Markdown，元数据人工维护；
- 本模块接受上传白名单内的二进制格式（pdf/docx/doc/xlsx/xlsm），元数据由
  CLI 参数统一指定（默认「公开」+ 指定部门），因此**只用于内部测试语料**，
  不暴露 HTTP 入口，也不应该用于客户文档——客户文档必须走复核。

被拒绝的情况（不会建行）：白名单外格式、解析失败、扫描件（scanned_pdf）、
空文档（empty_document）。扫描件必须等 OCR 能力上线，不能把空文本灌进索引。

用法：

    INGEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag \
      uv run python -m ingest.import_batch data/public_samples \
      --dept 公开资料 --classification 公开

    # 只解析不入库，先看会导入什么
    uv run python -m ingest.import_batch data/public_samples --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from psycopg import Connection

from app.users import CLASSIFICATION_LEVELS
from ingest import repository as repo
from ingest.config import get_settings
from ingest.errors import IngestError
from ingest.parsers import get_parser
from ingest.queue import enqueue
from ingest.sniff import sniff_file
from ingest.storage import FileStore

BASE_DIR = Path(__file__).resolve().parents[1]
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xlsm"}
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}
REJECT_CODES = {"scanned_pdf", "empty_document"}


def _text_chars(model) -> int:
    total = 0
    for block in model.blocks:
        if block.table is not None:
            total += sum(len(cell) for row in block.table.rows for cell in row)
        else:
            total += len(block.text or "")
    return total


def _duplicate(conn: Connection, sha256: str) -> dict | None:
    return conn.execute(
        """SELECT id AS version_id, document_id FROM doc_versions
            WHERE sha256=%s ORDER BY created_at LIMIT 1""",
        (sha256,),
    ).fetchone()


def import_file(conn: Connection, path: Path, *, dept: str, level: int,
                created_by: str, dry_run: bool = False) -> dict:
    settings = get_settings()
    result = {"file": path.name, "status": "skipped", "reason": "",
              "doc_id": None, "version_id": None, "chunks_queued": 0}

    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        result.update(status="rejected", reason=f"白名单外格式：{path.suffix}")
        return result

    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    # dry-run 没有连接，跳过重复检查（正式导入必须查，避免重复建行）
    existing = _duplicate(conn, sha256) if conn is not None else None
    if existing is not None:
        result.update(status="duplicate", reason="内容已存在",
                      version_id=str(existing["version_id"]))
        return result

    try:
        sniff = sniff_file(path, path.name)
        model = get_parser(sniff.kind).parse(path)
    except IngestError as exc:
        result.update(status="rejected", reason=f"{type(exc).__name__}: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断整批
        result.update(status="rejected", reason=f"{type(exc).__name__}: {exc}")
        return result

    codes = {warning.code for warning in model.warnings}
    blocking = sorted(codes & REJECT_CODES)
    if blocking:
        result.update(status="rejected", reason=f"解析结果不可入库：{', '.join(blocking)}")
        return result
    if _text_chars(model) < 50:
        result.update(status="rejected", reason="有效字符少于 50，拒绝入库")
        return result

    if dry_run:
        result.update(status="would_import", chunks_queued=0)
        return result

    warnings = [warning.model_dump() for warning in model.warnings]
    title = (model.meta.title or path.stem).strip()
    try:
        storage_path = FileStore(settings.upload_dir).put_file(path, sha256)
        doc = repo.create_document(
            conn, title=title, dept=dept, classification=level,
            effective_date=None, created_by=created_by, origin_id=None)
        version = repo.create_version(
            conn, document_id=doc["id"], version_no=1, sha256=sha256,
            filename=path.name, mime=MIME.get(sniff.kind, "application/octet-stream"),
            size_bytes=path.stat().st_size, storage_path=storage_path,
            parser=model.parser.name, parser_version=model.parser.version,
            status="embedding")
        repo.set_version_status(conn, version["id"], "embedding",
                                ir=model, warnings=warnings)
        enqueue(conn, "embed", {"version_id": str(version["id"])}, priority=50)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    result.update(status="imported", doc_id=str(doc["id"]),
                  version_id=str(version["id"]))
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(description="批量导入测试语料（trusted_import）")
    parser.add_argument("path", nargs="?", default=str(BASE_DIR / "data" / "public_samples"))
    parser.add_argument("--dept", default="公开资料")
    parser.add_argument("--classification", default="公开",
                        choices=list(CLASSIFICATION_LEVELS))
    parser.add_argument("--created-by", default="public-sample")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recursive", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        print(f"路径不存在：{root}")
        return 1
    if root.is_file():
        files = [root]
    else:
        pattern = "**/*" if args.recursive else "*"
        files = sorted(p for p in root.glob(pattern)
                       if p.is_file() and p.name != "manifest.json"
                       and ".part" not in p.suffix)
    if not files:
        print(f"未在 {root} 找到文件")
        return 1

    level = CLASSIFICATION_LEVELS[args.classification]
    results: list[dict] = []
    if args.dry_run:
        for path in files:
            results.append(import_file(None, path, dept=args.dept, level=level,
                                       created_by=args.created_by, dry_run=True))
    else:
        from ingest.db import connection

        with connection() as conn:
            for path in files:
                results.append(import_file(
                    conn, path, dept=args.dept, level=level,
                    created_by=args.created_by))

    for item in results:
        flag = {"imported": "+", "would_import": "?", "duplicate": "=",
                "rejected": "!", "skipped": "-"}.get(item["status"], "-")
        print(f"{flag} {item['file']}: {item['status']} {item['reason']}".rstrip())

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(f"\n汇总：{counts}")
    if not args.dry_run and counts.get("imported"):
        print("下一步：uv run python -m ingest.worker --once")
    return 1 if counts.get("rejected") else 0


if __name__ == "__main__":
    raise SystemExit(main())
