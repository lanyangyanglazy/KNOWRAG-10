"""导入现有 Markdown 语料（trusted_import 唯一入口，不经 HTTP）。

data/gov_docs/*.md 自带 front matter（doc_id/title/dept/classification/
effective_date），元数据已人工维护，因此跳过「上传 → 解析 → 复核」，
直接解析为 DocModel、把版本置为 embedding 并入队。之后由
embed → activate 两个 job 完成切片、向量化与激活。

用法：
    INGEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag \
      uv run python -m ingest.import_gov_docs [目录]
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from psycopg import Connection

from app.audit import write_audit
from app.users import CLASSIFICATION_LEVELS
from ingest import repository as repo
from ingest.config import Settings, get_settings
from ingest.parsers.markdown_parser import MarkdownParser, read_front_matter
from ingest.queue import enqueue
from ingest.storage import FileStore

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = BASE_DIR / "data" / "gov_docs"
REQUIRED_FIELDS = ("doc_id", "title", "dept", "classification")

logger = logging.getLogger("ingest.import")


def _effective_date(raw, filename: str) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(
            f"{filename} 的 effective_date 必须是 YYYY-MM-DD：{raw!r}") from exc


def _validate(meta: dict, filename: str) -> int:
    missing = [key for key in REQUIRED_FIELDS if not meta.get(key)]
    if missing:
        raise ValueError(f"{filename} 缺少必填元数据: {', '.join(missing)}")
    raw = meta["classification"]
    if raw not in CLASSIFICATION_LEVELS:
        raise ValueError(
            f"{filename} 的 classification={raw!r} 非法，"
            f"可选：{'/'.join(CLASSIFICATION_LEVELS)}")
    return CLASSIFICATION_LEVELS[raw]


def _duplicate(conn: Connection, sha256: str, path: Path) -> dict | None:
    """全库按内容去重：同一份文件重复导入只返回已有版本，不再建行。"""
    row = conn.execute(
        """SELECT id AS version_id, document_id FROM doc_versions
            WHERE sha256=%s ORDER BY created_at LIMIT 1""",
        (sha256,),
    ).fetchone()
    if row is None:
        return None
    return {
        "file": path.name, "doc_id": None,
        "document_id": str(row["document_id"]),
        "version_id": str(row["version_id"]),
        "job_id": None, "sha256": sha256, "skipped": True, "error": None,
    }


def import_file(conn: Connection, path: Path, *, created_by: str = "import",
                settings: Settings | None = None) -> dict:
    path = Path(path)
    settings = settings or get_settings()

    meta, _ = read_front_matter(path)
    level = _validate(meta, path.name)
    effective = _effective_date(meta.get("effective_date"), path.name)
    doc_id = str(meta["doc_id"])

    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    duplicate = _duplicate(conn, sha256, path)
    if duplicate is not None:
        duplicate["doc_id"] = doc_id
        return duplicate

    model = MarkdownParser(settings=settings).parse(path)
    try:
        storage_path = FileStore(settings.upload_dir).put_file(path, sha256)
        doc = repo.create_document(
            conn, title=str(meta["title"]), dept=str(meta["dept"]),
            classification=level, effective_date=effective,
            created_by=created_by)
        version = repo.create_version(
            conn, document_id=doc["id"], version_no=1, sha256=sha256,
            filename=path.name, mime="text/markdown",
            size_bytes=path.stat().st_size, storage_path=storage_path,
            parser=MarkdownParser.name, parser_version=MarkdownParser.version,
            status="embedding")
        repo.set_version_status(
            conn, version["id"], "embedding", ir=model,
            warnings=[w.model_dump() for w in model.warnings])
        job_id = enqueue(conn, "embed", {"version_id": str(version["id"])},
                         priority=50)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # 审计是证据而非事务参与者：文档/版本/任务已提交，审计失败不能把导入翻转成失败。
    try:
        write_audit({
            "event": "ingest.import", "action": "import", "actor": created_by,
            "document_id": str(doc["id"]), "version_id": str(version["id"]),
            "doc_id": doc_id, "file": path.name,
            "classification": meta["classification"], "sha256": sha256,
        })
    except Exception as exc:  # noqa: BLE001 - 导入已提交，审计失败只影响审计文件
        logger.warning("导入审计写入失败（数据已提交）：%s", exc)
    return {
        "file": path.name, "doc_id": doc_id,
        "document_id": str(doc["id"]), "version_id": str(version["id"]),
        "job_id": job_id, "sha256": sha256, "skipped": False, "error": None,
    }


def import_dir(conn: Connection, directory: Path, *, created_by: str = "import",
               settings: Settings | None = None) -> list[dict]:
    paths = sorted(Path(directory).glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"未在 {directory} 找到 Markdown 文档")

    results: list[dict] = []
    for path in paths:
        try:
            results.append(import_file(conn, path, created_by=created_by,
                                       settings=settings))
        except Exception as exc:  # noqa: BLE001 - 单个坏文件不拖垮整批
            conn.rollback()
            results.append({
                "file": path.name, "doc_id": None, "document_id": None,
                "version_id": None, "job_id": None, "sha256": None,
                "skipped": False, "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="导入现有 Markdown 语料到摄取库（trusted_import，不进复核）")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_CORPUS))
    parser.add_argument("--created-by", default="import")
    args = parser.parse_args(argv)

    # Windows 控制台默认 GBK，直接打印中文会 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    from ingest.db import connection

    with connection() as conn:
        results = import_dir(conn, Path(args.path), created_by=args.created_by)

    imported = [r for r in results if not r["skipped"] and not r["error"]]
    skipped = [r for r in results if r["skipped"]]
    failed = [r for r in results if r["error"]]
    print(f"导入 {len(imported)}，跳过 {len(skipped)}，失败 {len(failed)}")
    for item in failed:
        print(f"  ! {item['file']}: {item['error']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
