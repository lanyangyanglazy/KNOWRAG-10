from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from psycopg import Connection

from app.audit import write_audit
from ingest import repository as repo
from ingest.activation import activate_version, purge_document
from ingest.config import Settings, get_settings
from ingest.embedding import embed_version
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
    """返回命中的注入特征片段（最多 5 条），用于警告与审计。

    片段围绕命中位置截取（前 20 / 后 40 字符），保证触发词一定出现在
    警告与审计里，而不是被长段落的开头截断掉。
    """
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
            pos = text.lower().find(needle)
            if pos < 0:
                continue
            start = max(0, pos - 20)
            end = min(len(text), pos + len(needle) + 40)
            snippet = " ".join(text[start:end].split())
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


def _parse_file(conn: Connection, dept: str, version: dict, source: Path,
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
        parser = _parser_for(conn, sniff.kind, dept, settings)
        return parser.parse(path)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def handle_parse(conn: Connection, payload: dict) -> None:
    settings = get_settings()
    version = repo.get_version(conn, payload["version_id"])
    if version is None:
        raise ValueError(f"版本不存在: {payload['version_id']}")
    # 幂等护栏：reap_stale 重投可能让同一个 version 的 parse job 被再次执行。
    # 已解析完成（待复核/已入嵌入/已就绪/已隔离）的版本不得回退状态或重复入队 embed。
    if version["status"] in {"needs_review", "embedding", "ready", "quarantined"}:
        return
    doc = repo.get_document(conn, version["document_id"])
    if doc is None:
        raise ValueError(f"文档不存在: {version['document_id']}")
    repo.set_version_status(conn, version["id"], "parsing")
    conn.commit()

    source = FileStore(settings.upload_dir).resolve(version["storage_path"])
    ir = _parse_file(conn, doc["dept"], version, source, settings)
    warnings = [w.model_dump() for w in ir.warnings]

    injection_hits = scan_injection(ir)
    if injection_hits:
        warnings.append({"code": "prompt_injection_suspected",
                         "detail": "；".join(injection_hits)})
        # 注入检测按设计「只告警不阻断」，且警告已随 versions.warnings 持久化；
        # 审计 JSONL 写失败不能让入库重试到死信，故 fail-open。
        try:
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
        except Exception as exc:  # noqa: BLE001 - 审计失败不阻断入库
            warnings.append({"code": "audit_write_failed",
                             "detail": f"prompt_injection_suspected 审计写入失败: {exc}"})

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
    """死信钩子：run_forever 对钩子异常没有兜底，本函数绝不外抛。

    先把证据落库（versions.error + parse_warning 复核项）并提交，审计失败
    只影响审计文件，不影响已持久化的证据。
    """
    version_id = job["payload"]["version_id"]
    try:
        row = conn.execute("SELECT last_error FROM jobs WHERE id=%s",
                           (job["id"],)).fetchone()
        error = (row or {}).get("last_error") or "parse failed"
        repo.set_version_status(conn, version_id, "failed", error=error)
        repo.ensure_review_item(conn, version_id, "parse_warning",
                                {"reason": "parse_failed", "error": error})
        conn.commit()
    except Exception:  # noqa: BLE001 - 钩子不得外抛，否则杀死 worker 循环
        conn.rollback()
        return
    try:
        write_audit({"action": "parse_failed", "version_id": str(version_id),
                     "detail": error})
    except Exception:  # noqa: BLE001 - 证据已由 versions.error 与复核项持久化
        pass


def handle_embed(conn: Connection, payload: dict) -> None:
    embed_version(conn, payload["version_id"])


def _job_error(conn: Connection, job_id: int, default: str) -> str:
    row = conn.execute("SELECT last_error FROM jobs WHERE id=%s",
                       (job_id,)).fetchone()
    return (row or {}).get("last_error") or default


def _fail_version(conn: Connection, job: dict, *, reason: str,
                  default: str) -> str | None:
    """把版本标 failed 并生成 parse_warning 复核项，供嵌入/激活死信共用。

    返回错误文本用于审计；数据库写入失败返回 None（连审计也放弃）。
    文档已被 purge 时版本行不存在，必须跳过 INSERT 复核项——外键错误
    会逃出 run_job 直接杀死 worker 循环。本函数绝不外抛。
    """
    version_id = job["payload"]["version_id"]
    try:
        error = _job_error(conn, job["id"], default)
        if repo.get_version(conn, version_id) is None:
            return error
        repo.set_version_status(conn, version_id, "failed", error=error)
        repo.ensure_review_item(conn, version_id, "parse_warning",
                                {"reason": reason, "error": error})
        conn.commit()
        return error
    except Exception:  # noqa: BLE001 - 钩子不得外抛，否则杀死 worker 循环
        conn.rollback()
        return None


def handle_embed_dead_letter(conn: Connection, job: dict) -> None:
    error = _fail_version(conn, job, reason="embed_failed", default="embed failed")
    if error is None:
        return
    try:
        write_audit({"action": "embed_failed",
                     "version_id": str(job["payload"]["version_id"]),
                     "detail": error})
    except Exception:  # noqa: BLE001 - 证据已由 versions.error 与复核项持久化
        pass


def handle_activate(conn: Connection, payload: dict) -> None:
    activate_version(conn, payload["version_id"])


def handle_activate_dead_letter(conn: Connection, job: dict) -> None:
    error = _fail_version(conn, job, reason="activate_failed",
                          default="activate failed")
    if error is None:
        return
    try:
        write_audit({"action": "activate_failed",
                     "version_id": str(job["payload"]["version_id"]),
                     "detail": error})
    except Exception:  # noqa: BLE001 - 证据已由 versions.error 与复核项持久化
        pass


def handle_purge(conn: Connection, payload: dict) -> None:
    purge_document(conn, payload["document_id"])


def handle_purge_dead_letter(conn: Connection, job: dict) -> None:
    # 文档行仍在（墓碑），但切片/版本可能已删一半；只记录审计供人工重跑 purge。
    # 不写数据库：进死信时 worker 已 rollback，且复发原因也可能来自数据库本身。
    try:
        write_audit({"action": "delete",
                     "doc_id": job["payload"].get("document_id"),
                     "detail": {"stage": "purge_failed", "job_id": job["id"],
                                "error": _job_error(conn, job["id"],
                                                    "purge failed")}})
    except Exception:  # noqa: BLE001 - 钩子不得外抛
        pass


HANDLERS = {
    "parse": handle_parse,
    "embed": handle_embed,
    "activate": handle_activate,
    "purge": handle_purge,
}
DEAD_LETTER_HOOKS = {
    "parse": handle_parse_dead_letter,
    "embed": handle_embed_dead_letter,
    "activate": handle_activate_dead_letter,
    "purge": handle_purge_dead_letter,
}
