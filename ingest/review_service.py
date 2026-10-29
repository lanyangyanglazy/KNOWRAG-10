from __future__ import annotations

import re

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.users import User
from ingest import repository as repo
from ingest.config import Settings, get_settings
from ingest.errors import Forbidden, NotFound, ReviewRequired
from ingest.ir import DocModel, Table
from ingest.parsers.table_build import rebuild_table
from ingest.parsers.templates import normalize_header_name
from ingest.queue import enqueue


def load_version_for_review(conn: Connection, version_id: str, user: User, *,
                            write: bool = False) -> tuple[dict, dict]:
    version = repo.get_version(conn, version_id)
    if version is None:
        raise NotFound("版本不存在")
    if write:
        # 复核写操作必须按版本串行：先锁定版本行，再基于锁定后的状态做判定，
        # 否则并发 confirm/approve 会各自读到旧的 needs_review 状态。
        locked = conn.execute(
            "SELECT * FROM doc_versions WHERE id=%s FOR UPDATE", (version_id,)
        ).fetchone()
        if locked is None:
            raise NotFound("版本不存在")
        version = dict(locked)
    doc = repo.get_document(conn, version["document_id"])
    if doc is None or doc["deleted_at"] is not None:
        raise NotFound("文档不存在")
    if not {"reviewer", "admin"} & set(user.roles):
        raise Forbidden("无复核权限")
    if "admin" not in user.roles:
        # 密级高于用户上限时与「不存在」同样处理，避免用 403/404 差异探测秘密文档。
        if doc["classification"] is not None and doc["classification"] > user.max_level:
            raise NotFound("版本不存在")
        if doc["dept"] != user.dept:
            raise NotFound("版本不存在")
    if write and version["status"] not in ("needs_review", "failed"):
        raise ReviewRequired(f"版本状态为 {version['status']}，不可复核")
    return version, doc


def _ir_of(version: dict) -> DocModel:
    if not version["ir"]:
        raise ReviewRequired("版本没有解析结果")
    return DocModel.model_validate(version["ir"])


def summary(conn: Connection, version_id: str, user: User) -> dict:
    version, doc = load_version_for_review(conn, version_id, user)
    ir = DocModel.model_validate(version["ir"]) if version["ir"] else None
    tables = []
    if ir is not None:
        for table in ir.tables():
            tables.append({
                "table_key": table.table_key, "sheet": table.sheet,
                "range": table.range, "header_rows": table.header_rows,
                "header_values": table.header_values,
                "columns": [c.model_dump() for c in table.columns],
                "footer_rows": table.footer_rows, "confidence": table.confidence,
                "source": table.source, "flags": table.flags,
                "candidates": table.candidates[:3],
                "grid": table.full_grid()[:30],
            })
    text = [b.text for b in (ir.blocks if ir else [])
            if b.type in ("heading", "paragraph") and b.text]
    return {
        "version_id": version_id, "status": version["status"],
        "document": doc, "parser": version["parser"],
        "warnings": version["warnings"], "tables": tables,
        "text_preview": text[:50],
        "open_items": repo.list_open_review_items(conn, version_id),
    }


def _apply_table_patches(ir: DocModel, tables: list, settings: Settings) -> None:
    patches = {patch.table_key: patch for patch in tables}
    known_keys = {table.table_key for table in ir.tables()}
    unknown = [key for key in patches if key not in known_keys]
    if unknown:
        raise ReviewRequired(f"表格不存在: {unknown[0]}")
    for block in ir.blocks:
        table = block.table
        if table is None or table.table_key not in patches:
            continue
        patch = patches[table.table_key]
        columns = []
        for index, column in enumerate(patch.columns):
            config = column.model_dump()
            # 前端编辑列时可以不回传 key：沿用原列 key，避免重建出非法空 key。
            if not config.get("key"):
                config["key"] = (table.columns[index].key
                                 if index < len(table.columns) else f"c{index + 1}")
            columns.append(config)
        try:
            rebuilt, _ = rebuild_table(
                table, patch.header_rows, columns, settings=settings,
            )
        except ValueError as exc:
            raise ReviewRequired(f"表格重切失败：{exc}") from exc
        if patch.footer_rows:
            data_end = rebuilt.data_start_row + len(rebuilt.rows) - 1
            for row in patch.footer_rows:
                if (row < rebuilt.data_start_row or row > data_end
                        or row in rebuilt.header_rows):
                    raise ReviewRequired("footer_rows 必须落在数据区")
            # 走完整校验重建，不用 model_copy 绕过 footer/表头重叠等约束检查。
            # 校验器还会拦 footer_rows 与 repeated_header_rows 重叠：这里只换了
            # footer_rows，失败必须转成 422 复核提示，不能让 ValidationError 漏成 500。
            try:
                rebuilt = Table.model_validate({
                    **rebuilt.model_dump(),
                    "footer_rows": sorted(set(patch.footer_rows)),
                })
            except ValidationError as exc:
                raise ReviewRequired(
                    "footer_rows 不能与表头或重复表头行重叠") from exc
        block.table = rebuilt


def _resolve_open(conn: Connection, version_id: str, kinds: set[str],
                  user: User) -> None:
    for item in repo.list_open_review_items(conn, version_id):
        if item["kind"] in kinds:
            repo.resolve_review_item(conn, item["id"], resolved_by=user.username)


def _start_embedding(conn: Connection, version_id: str, ir: DocModel,
                     warnings: list, user: User) -> None:
    repo.set_version_status(conn, version_id, "embedding", ir=ir, warnings=warnings)
    enqueue(conn, "embed", {"version_id": version_id}, priority=50)


def confirm(conn: Connection, version_id: str, payload, *, user: User,
            settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    version, doc = load_version_for_review(conn, version_id, user, write=True)
    # 必须先记录复核前的密级：本次 payload 可能把秘密级改为低密级，
    # 改后再看 doc 就丢了「这是降密」这一事实，从而绕过双人确认。
    previous = doc["classification"]
    ir = _ir_of(version)
    _apply_table_patches(ir, payload.tables, settings)
    ir = DocModel.model_validate(ir.model_dump(mode="json"))

    updates: dict = {}
    meta = payload.meta
    if meta.title:
        updates["title"] = meta.title
    if meta.dept:
        if "admin" not in user.roles and meta.dept != user.dept:
            raise Forbidden("只能复核本部门文档")
        updates["dept"] = meta.dept
    if meta.classification is not None:
        if meta.classification > user.max_level:
            raise Forbidden("密级超过你的权限")
        updates["classification"] = meta.classification
    if meta.effective_date:
        updates["effective_date"] = meta.effective_date
    if updates:
        repo.set_document_meta(conn, doc["id"], **updates)
    doc = repo.get_document(conn, doc["id"])
    if doc["classification"] is None:
        raise ReviewRequired("必须先设置密级才能激活")

    _resolve_open(conn, version_id, {"structure", "parse_warning"}, user)

    if payload.apply_template and payload.tables:
        # 先在同一事务里回写重切后的 IR，后面保存模板时读到的才是人工确认结果。
        repo.set_version_status(conn, version_id, version["status"], ir=ir,
                                warnings=version["warnings"])
        table = payload.tables[0]
        save_template(conn, version_id, table_key=table.table_key,
                      name=f"{doc['title']}-{table.table_key}",
                      scope_dept=doc["dept"], user=user, _commit=False)

    open_classification = [i for i in repo.list_open_review_items(conn, version_id)
                           if i["kind"] == "classification"]
    # 降密已写入文档后 previous 不再可见；只要秘密级复核项还挂着未完成的双人票，
    # 就必须继续走双人分支，避免同一人再 confirm 一次就被放行。
    secret_pending = any(
        (item["payload"] or {}).get("reason") in ("secret_two_person", "secret_downgrade")
        or repo.count_approvals(conn, item["id"]) > 0
        for item in open_classification
    )
    if doc["classification"] == 2 or (
            previous == 2 and doc["classification"] < 2) or secret_pending:
        # 秘密级，或从秘密级降密：都要求两名不同用户确认，防止单人降密绕过。
        reason = ("secret_two_person" if doc["classification"] == 2
                  else "secret_downgrade")
        item_id = (open_classification[0]["id"] if open_classification
                   else repo.ensure_review_item(
                       conn, version_id, "classification", {"reason": reason}))
        # add_approval 对同一用户重复确认不插入新票，pending 以实际票数为准。
        repo.add_approval(conn, item_id, user.username)
        approvals = repo.count_approvals(conn, item_id)
        if approvals < 2:
            repo.set_version_status(conn, version_id, "needs_review", ir=ir,
                                    warnings=version["warnings"])
            conn.commit()
            return {"version_id": version_id, "status": "needs_review",
                    "pending_approvals": approvals,
                    "open_items": len(repo.list_open_review_items(conn, version_id)),
                    "document_id": str(doc["id"]),
                    "classification": doc["classification"]}
        repo.resolve_review_item(conn, item_id, resolved_by=user.username)
    else:
        _resolve_open(conn, version_id, {"classification"}, user)

    items = repo.list_open_review_items(conn, version_id)
    if items:
        repo.set_version_status(conn, version_id, "needs_review", ir=ir,
                                warnings=version["warnings"])
        conn.commit()
        return {"version_id": version_id, "status": "needs_review",
                "pending_approvals": 0, "open_items": len(items),
                "document_id": str(doc["id"]),
                "classification": doc["classification"]}
    _start_embedding(conn, version_id, ir, version["warnings"], user)
    return {"version_id": version_id, "status": "embedding",
            "pending_approvals": 0, "open_items": 0,
            "document_id": str(doc["id"]),
            "classification": doc["classification"]}


def approve(conn: Connection, version_id: str, *, user: User) -> dict:
    version, doc = load_version_for_review(conn, version_id, user, write=True)
    items = [i for i in repo.list_open_review_items(conn, version_id)
             if i["kind"] == "classification"]
    # 降密已落库后文档不再是秘密级，但挂着的密级复核项仍需第二票才能完成；
    # 只有确实存在这类复核项才放行，普通文档仍拒绝走 /approve。
    secret_item = any(
        (item["payload"] or {}).get("reason") in ("secret_two_person", "secret_downgrade")
        for item in items
    )
    if doc["classification"] != 2 and not secret_item:
        raise ReviewRequired("只有秘密级需要二次确认")
    if user.max_level < 2:
        raise Forbidden("无秘密级确认权限")
    if not items:
        raise ReviewRequired("没有待确认的密级复核项")
    item_id = items[0]["id"]
    if not repo.add_approval(conn, item_id, user.username):
        raise Forbidden("同一用户不能重复确认")
    approvals = repo.count_approvals(conn, item_id)
    if approvals < 2:
        conn.commit()
        return {"version_id": version_id, "status": "needs_review",
                "pending_approvals": approvals,
                "document_id": str(doc["id"]),
                "classification": doc["classification"]}
    repo.resolve_review_item(conn, item_id, resolved_by=user.username)

    remaining = repo.list_open_review_items(conn, version_id)
    if remaining:
        conn.commit()
        return {"version_id": version_id, "status": "needs_review",
                "pending_approvals": approvals, "open_items": len(remaining),
                "document_id": str(doc["id"]),
                "classification": doc["classification"]}
    ir = _ir_of(version)
    _start_embedding(conn, version_id, ir, version["warnings"], user)
    return {"version_id": version_id, "status": "embedding",
            "pending_approvals": approvals, "open_items": 0,
            "document_id": str(doc["id"]),
            "classification": doc["classification"]}


def waive(conn: Connection, version_id: str, *, reason: str,
          item_id: int | None = None, user: User) -> dict:
    if not reason.strip():
        raise ReviewRequired("waive 必须填写理由")
    version, doc = load_version_for_review(conn, version_id, user, write=True)
    if doc["classification"] == 2:
        raise Forbidden("秘密级不可 waive")
    items = repo.list_open_review_items(conn, version_id)
    targets = [i for i in items if item_id is None or i["id"] == item_id]
    if not targets:
        raise NotFound("复核项不存在")
    # 先整体校验再落库：目标里只要有一个密级项就整批拒绝，避免部分 waive 后才发现。
    if any(item["kind"] == "classification" for item in targets):
        raise ReviewRequired("密级复核不可 waive")
    for item in targets:
        repo.resolve_review_item(conn, item["id"], status="waived",
                                 resolved_by=user.username, note=reason)
    remaining = repo.list_open_review_items(conn, version_id)
    if remaining:
        conn.commit()
        return {"version_id": version_id, "status": "needs_review",
                "open_items": len(remaining),
                "document_id": str(doc["id"]),
                "classification": doc["classification"]}
    ir = _ir_of(version)
    _start_embedding(conn, version_id, ir, version["warnings"], user)
    return {"version_id": version_id, "status": "embedding", "open_items": 0,
            "document_id": str(doc["id"]),
            "classification": doc["classification"]}


def _signature_of(table: Table) -> dict:
    return {
        "ncols": len(table.columns),
        "header_depth": len(table.header_rows),
        "relative_header_row": (table.header_rows[0] - table.range_start_row
                                if table.header_rows else 0),
        "columns_norm": [normalize_header_name(c.name) for c in table.columns],
        "sheet_pattern": f"^{re.escape(table.sheet)}$" if table.sheet else None,
        "sheet_name": table.sheet,
    }


def save_template(conn: Connection, version_id: str, *, table_key: str,
                  name: str, scope_dept: str | None, user: User,
                  _commit: bool = True) -> dict:
    version, doc = load_version_for_review(conn, version_id, user)
    if scope_dept is None:
        # 默认归属文档部门（管理员也如此），避免后台创建的模板意外全部门可见。
        scope_dept = doc["dept"]
    elif "admin" not in user.roles and scope_dept != user.dept:
        raise Forbidden("只有管理员可以创建跨部门模板")
    ir = _ir_of(version)
    table = next((t for t in ir.tables() if t.table_key == table_key), None)
    if table is None:
        raise NotFound("表格不存在")
    config = {
        "header_rows": table.header_rows,
        "columns": [c.model_dump() for c in table.columns],
        "footer_rows": table.footer_rows,
    }
    row = conn.execute(
        """INSERT INTO layout_templates (name, scope_dept, signature, config, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (name, scope_dept, Jsonb(_signature_of(table)), Jsonb(config), user.username),
    ).fetchone()
    if _commit:
        conn.commit()
    return dict(row)
