"""
pg 后端的评测语料视图。

FAISS 后端可以直接从 `service.documents` 拿到全部切片；pg 后端的切片在库里，
必须按「已激活文档的 active 切片」读出，才能复用同一套金标校验逻辑。

这里故意不做权限裁剪：评测台属于运维侧，金标校验问的是「语料里有没有这段证据」，
不是「某个演示用户能不能看到」。权限相关的召回由用例的 `user` + `forbidden_doc_ids` 表达。
"""

from __future__ import annotations

from typing import Sequence

from langchain_core.documents import Document as LCDocument
from psycopg import Connection

from evaluation.cases import EvalCase, validate_against_corpus

_ACTIVE_CHUNKS_SQL = """
SELECT c.document_id, c.content, c.metadata,
       COALESCE(d.origin_id, d.id::text) AS doc_id
  FROM chunks c
  JOIN documents d ON d.id = c.document_id
 WHERE c.active
   AND d.status = 'active'
   AND d.deleted_at IS NULL
 ORDER BY c.document_id, c.ordinal
"""


def load_active_chunks(conn: Connection) -> list[LCDocument]:
    """读出所有可检索切片，形状与 FAISS 的 LCDocument 一致（page_content + metadata）。"""
    rows = conn.execute(_ACTIVE_CHUNKS_SQL).fetchall()
    documents: list[LCDocument] = []
    for row in rows:
        metadata = dict(row["metadata"] or {})
        # doc_id 是金标匹配的唯一依据，以列值为准（导入语料取源系统编号），
        # 避免 metadata 缺字段或写旧值。
        metadata["doc_id"] = row["doc_id"]
        documents.append(
            LCDocument(page_content=row["content"], metadata=metadata))
    return documents


def validate_against_pg(cases: Sequence[EvalCase], conn: Connection) -> list[str]:
    """在 pg 语料上校验金标；数据库为空时给出可执行的修复提示。"""
    documents = load_active_chunks(conn)
    if not documents:
        return [
            "数据库里没有任何已激活切片：先执行 `uv run python -m ingest.import_gov_docs`，"
            "再用 `uv run python -m ingest.worker --once` 跑完 embed/activate 任务"
        ]
    return validate_against_corpus(cases, documents)
