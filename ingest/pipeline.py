from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from psycopg import Connection

from app.audit import write_audit
from ingest import repository as repo
from ingest.config import Settings, get_settings
from ingest.errors import Quarantined
from ingest.parsers import get_parser
from ingest.queue import enqueue
from ingest.sniff import sniff_file, strip_macros
from ingest.storage import FileStore

_STRUCTURE_CODES = {"table_no_header", "ambiguous_header", "scanned_pdf", "empty_document"}

# 文档正文是不可信输入（设计 §10.3）：命中特征只记警告并审计，不阻断入库。
# 真正的防线在生成侧——上下文用固定分隔符包裹，系统提示声明「资料不是指令」。
_INJECTION_PATTERNS = (
    "忽略以上指令", "忽略之前", "忽略上述", "ignore previous instructions",
    "ignore all previous", "disregard previous", "reveal the system prompt",
    "系统提示词", "输出你的设定", "developer mode",
)


def scan_injection(ir) -> list[str]:
    """返回命中的注入特征片段（最多 5 条），用于警告与审计。"""
    texts: list[str] = []
    for block in ir.blocks:
        if block.text:
            texts.append(block.text)
        if block.table is not None:
            for row in block.table.header_values + block.table.rows:
                texts.extend(row)
    hits: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        needle = pattern.lower()
        for text in texts:
            if needle in text.lower():
                snippet = " ".join(text.split())[:80]
                if snippet not in hits:
                    hits.append(snippet)
                break
        if len(hits) >= 5:
            break
    return hits


def _needs_structure_review(ir, settings: Settings) -> bool:
    if any(w.code in _STRUCTURE_CODES for w in ir.warnings):
        return True
    return any(t.confidence < settings.header_accept for t in ir.tables())


def _parser_for(conn: Connection, kind: str, dept: str, settings: Settings):
    # markdown 解析器只接受 settings（无模板/LLM 兜底），单独传参避免 TypeError。
    if kind == "markdown":
        return get_parser("markdown", settings=settings)
    templates = repo.list_templates(conn, dept)
    llm = None
    if settings.header_llm_enabled:
        import main as core
        llm = core.init_llm()
    return get_parser(kind, templates=templates, llm=llm, settings=settings)


def _parse_file(conn: Connection, version: dict, source: Path,
                settings: Settings):
    sniff = sniff_file(source, version["filename"], settings)
    path = source
    tmpdir = None
    if sniff.macro:
        tmpdir = tempfile.mkdtemp(prefix="rag-macro-")
        stripped = Path(tmpdir) / Path(version["filename"]).name
        if not strip_macros(source, stripped):
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise Quarantined("宏剥离失败")
        path = stripped
    try:
        dept = _dept_of(conn, version)
        parser = _parser_for(conn, sniff.kind, dept, settings)
        return parser.parse(path)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _dept_of(conn: Connection, version: dict) -> str:
    doc = repo.get_document(conn, version["document_id"])
    return doc["dept"]


def handle_parse(conn: Connection, payload: dict) -> None:
    settings = get_settings()
    version = repo.get_version(conn, payload["version_id"])
    if version is None:
        raise ValueError(f"版本不存在: {payload['version_id']}")
    doc = repo.get_document(conn, version["document_id"])
    repo.set_version_status(conn, version["id"], "parsing")
    conn.commit()

    source = FileStore(settings.upload_dir).resolve(version["storage_path"])
    ir = _parse_file(conn, version, source, settings)
    warnings = [w.model_dump() for w in ir.warnings]

    injection_hits = scan_injection(ir)
    if injection_hits:
        warnings.append({"code": "prompt_injection_suspected",
                         "detail": "；".join(injection_hits)})
        # 审计落盘是 JSON：UUID 必须转字符串，否则 json.dumps 抛 TypeError。
        write_audit({
            "action": "prompt_injection_suspected",
            "user": "worker",
            "dept": doc["dept"],
            "doc_id": str(doc["id"]),
            "version_id": str(version["id"]),
            "classification": doc["classification"],
            "detail": injection_hits,
        })

    if not payload.get("trusted_import"):
        if _needs_structure_review(ir, settings):
            repo.ensure_review_item(conn, version["id"], "structure", {
                "reason": "structure",
                "tables": [t.model_dump(mode="json") for t in ir.tables()],
                "warnings": [w.code for w in ir.warnings],
            })
        if doc["classification"] is None:
            repo.ensure_review_item(conn, version["id"], "classification",
                                    {"reason": "unclassified"})
        elif doc["classification"] == 2:
            repo.ensure_review_item(conn, version["id"], "classification",
                                    {"reason": "secret_two_person"})
    elif doc["classification"] is None:
        raise ValueError("trusted_import 必须先在 documents 上定密级")

    items = repo.list_open_review_items(conn, version["id"])
    if items:
        repo.set_version_status(conn, version["id"], "needs_review",
                                ir=ir, warnings=warnings)
        conn.commit()
        return
    repo.set_version_status(conn, version["id"], "embedding", ir=ir, warnings=warnings)
    # jobs.payload 是 jsonb：UUID 以字符串入队，避免 JSON 序列化失败。
    enqueue(conn, "embed", {"version_id": str(version["id"])}, priority=50)


def handle_parse_dead_letter(conn: Connection, job: dict) -> None:
    version_id = job["payload"]["version_id"]
    row = conn.execute("SELECT last_error FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    error = (row or {}).get("last_error") or "parse failed"
    repo.set_version_status(conn, version_id, "failed", error=error)
    repo.ensure_review_item(conn, version_id, "parse_warning",
                            {"reason": "parse_failed", "error": error})
    conn.commit()
    write_audit({"action": "parse_failed", "version_id": str(version_id),
                 "detail": error})


HANDLERS = {"parse": handle_parse}
DEAD_LETTER_HOOKS = {"parse": handle_parse_dead_letter}
