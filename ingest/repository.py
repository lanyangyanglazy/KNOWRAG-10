from __future__ import annotations

from datetime import date

from psycopg import Connection
from psycopg.types.json import Jsonb

_DOC_FIELDS = {"title", "dept", "classification", "effective_date"}


def create_document(conn: Connection, *, title: str, dept: str,
                    classification: int | None, effective_date: date | str | None,
                    created_by: str, origin_id: str | None = None) -> dict:
    return conn.execute(
        """INSERT INTO documents
           (title, dept, classification, effective_date, created_by, origin_id)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (title, dept, classification, effective_date, created_by, origin_id),
    ).fetchone()


def get_document(conn: Connection, document_id: str) -> dict | None:
    return conn.execute("SELECT * FROM documents WHERE id=%s", (document_id,)).fetchone()


def lock_document(conn: Connection, document_id: str) -> dict | None:
    return conn.execute("SELECT * FROM documents WHERE id=%s FOR UPDATE",
                        (document_id,)).fetchone()


def next_version_no(conn: Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM doc_versions WHERE document_id=%s",
        (document_id,),
    ).fetchone()
    return int(row["n"])


def create_version(conn: Connection, *, document_id: str, version_no: int,
                   sha256: str, filename: str, mime: str, size_bytes: int,
                   storage_path: str, parser: str, parser_version: str,
                   status: str = "uploaded") -> dict:
    return conn.execute(
        """INSERT INTO doc_versions
           (document_id, version_no, sha256, filename, mime, size_bytes,
            storage_path, parser, parser_version, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (document_id, version_no, sha256, filename, mime, size_bytes,
         storage_path, parser, parser_version, status),
    ).fetchone()


def get_version(conn: Connection, version_id: str) -> dict | None:
    return conn.execute("SELECT * FROM doc_versions WHERE id=%s", (version_id,)).fetchone()


def lock_version(conn: Connection, version_id: str) -> dict | None:
    return conn.execute("SELECT * FROM doc_versions WHERE id=%s FOR UPDATE",
                        (version_id,)).fetchone()


def list_versions(conn: Connection, document_id: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM doc_versions WHERE document_id=%s ORDER BY version_no DESC",
        (document_id,),
    ).fetchall()


# 详情接口用的列投影：排除 ir（可能很大且含内部结构）与 storage_path（落盘路径不外泄）。
_VERSION_SUMMARY_FIELDS = """
    id, document_id, version_no, sha256, filename, mime, size_bytes,
    parser, parser_version, status, warnings, error, created_at, activated_at
"""


def list_version_summaries(conn: Connection, document_id: str) -> list[dict]:
    return conn.execute(
        f"""SELECT {_VERSION_SUMMARY_FIELDS} FROM doc_versions
            WHERE document_id=%s ORDER BY version_no DESC""",
        (document_id,),
    ).fetchall()


def set_version_status(conn: Connection, version_id: str, status: str, *,
                       ir: object = None, warnings: list | None = None,
                       error: str | None = None) -> None:
    sets = ["status=%s"]
    params: list = [status]
    if ir is not None:
        sets.append("ir=%s")
        params.append(Jsonb(ir.model_dump(mode="json") if hasattr(ir, "model_dump") else ir))
    if warnings is not None:
        sets.append("warnings=%s")
        params.append(Jsonb(warnings))
    if error is not None:
        sets.append("error=%s")
        params.append(error)
    params.append(version_id)
    conn.execute(f"UPDATE doc_versions SET {', '.join(sets)} WHERE id=%s", params)


def set_document_meta(conn: Connection, document_id: str, **fields) -> None:
    updates = {k: v for k, v in fields.items() if k in _DOC_FIELDS}
    if not updates:
        return
    sets = ", ".join(f"{k}=%s" for k in updates)
    conn.execute(
        f"UPDATE documents SET {sets}, updated_at=now() WHERE id=%s",
        (*updates.values(), document_id),
    )


def ensure_review_item(conn: Connection, version_id: str, kind: str,
                       payload: dict) -> int:
    reason = payload.get("reason")
    # payload->>'reason' 为 NULL 时不能用 `= NULL`（永远不命中），必须用 IS NULL。
    if reason is None:
        row = conn.execute(
            """SELECT id FROM review_items
               WHERE version_id=%s AND kind=%s AND status='open'
                 AND payload->>'reason' IS NULL ORDER BY id LIMIT 1""",
            (version_id, kind),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT id FROM review_items
               WHERE version_id=%s AND kind=%s AND status='open'
                 AND payload->>'reason' = %s ORDER BY id LIMIT 1""",
            (version_id, kind, reason),
        ).fetchone()
    if row:
        return int(row["id"])
    created = conn.execute(
        """INSERT INTO review_items (version_id, kind, payload)
           VALUES (%s, %s, %s) RETURNING id""",
        (version_id, kind, Jsonb(payload)),
    ).fetchone()
    return int(created["id"])


def list_open_review_items(conn: Connection, version_id: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM review_items WHERE version_id=%s AND status='open' ORDER BY id",
        (version_id,),
    ).fetchall()


def resolve_review_item(conn: Connection, item_id: int, *, status: str = "resolved",
                        resolved_by: str, note: str | None = None) -> None:
    conn.execute(
        """UPDATE review_items SET status=%s, resolved_by=%s, resolved_at=now(), note=%s
           WHERE id=%s AND status='open'""",
        (status, resolved_by, note, item_id),
    )


def add_approval(conn: Connection, item_id: int, user_id: str, decision: str = "approve",
                 note: str | None = None) -> bool:
    row = conn.execute(
        """INSERT INTO review_approvals (review_item_id, user_id, decision, note)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (review_item_id, user_id) DO NOTHING
           RETURNING id""",
        (item_id, user_id, decision, note),
    ).fetchone()
    return row is not None


def count_approvals(conn: Connection, item_id: int, decision: str = "approve") -> int:
    return int(conn.execute(
        "SELECT count(*) AS n FROM review_approvals WHERE review_item_id=%s AND decision=%s",
        (item_id, decision),
    ).fetchone()["n"])


def list_templates(conn: Connection, dept: str) -> list[dict]:
    return conn.execute(
        """SELECT * FROM layout_templates
           WHERE scope_dept IS NULL OR scope_dept=%s ORDER BY updated_at DESC""",
        (dept,),
    ).fetchall()
