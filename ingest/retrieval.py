from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from pgvector import Vector
from psycopg import Connection

from app.users import User
from ingest.chunking import tokenize
from ingest.config import Settings, get_settings
from ingest.embedding import Embedder, get_embedder
from ingest.errors import ModelUnavailable

RRF_K = 60
NEIGHBOR_LIMIT = 2
DEFAULT_MAX_DISTANCE = 0.78

# tsquery 里只保留词/数字/下划线；jieba 切出的标点直接丢弃
_TS_SAFE = re.compile(r"[^\w]+", re.UNICODE)

_BASE = """
SELECT c.id AS db_id, c.document_id, c.version_id, c.ordinal, c.chunk_type,
       c.content, c.metadata, c.dept, c.classification, v.version_no,
       d.title AS doc_title, d.effective_date
  FROM chunks c
  JOIN documents d ON d.id = c.document_id
  JOIN doc_versions v ON v.id = c.version_id
"""

_ACL = """
c.active
AND d.status = 'active' AND d.deleted_at IS NULL
AND c.classification IS NOT NULL
AND c.classification <= %s
AND (c.classification = 0 OR c.dept = %s)
"""


@dataclass
class Hit:
    db_id: int
    chunk_id: str
    doc_id: str
    version_id: str
    ordinal: int
    chunk_type: str
    content: str
    title: str
    dept: str
    classification: int
    effective_date: str
    metadata: dict
    distance: float | None = None
    expanded: bool = False


@dataclass
class RetrievalOutcome:
    query: str
    hits: list[Hit] = field(default_factory=list)
    best_distance: float | None = None
    dense_hits: int = 0
    sparse_hits: int = 0
    visible_docs: int = 0
    visible_chunks: int = 0
    grounded: bool = False
    reason: str = ""


def _threshold(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    raw = os.getenv("RAG_MAX_DISTANCE")
    if not raw or not raw.strip():
        return DEFAULT_MAX_DISTANCE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DISTANCE
    return value if value > 0 else DEFAULT_MAX_DISTANCE


def _tsquery(query: str) -> str | None:
    raw = " ".join(tokenize(query))
    terms = [t for t in _TS_SAFE.sub(" ", raw).split() if t]
    if not terms:
        return None
    return " | ".join(f"'{term}'" for term in terms)


def _hit(row: dict, *, distance: float | None = None,
         expanded: bool = False) -> Hit:
    metadata = row["metadata"] or {}
    chunk_id = metadata.get("chunk_id") or (
        f"{row['document_id']}@v{row['version_no']}#{row['ordinal']}")
    # 导入语料的切片 metadata 带源系统编号（gov-xxx），检索/引用/评测统一用它；
    # 上传文档 metadata 的 doc_id 就是 UUID，旧切片没有该键时回退文档主键。
    doc_id = metadata.get("doc_id") or str(row["document_id"])
    return Hit(
        db_id=int(row["db_id"]), chunk_id=chunk_id,
        doc_id=str(doc_id), version_id=str(row["version_id"]),
        ordinal=int(row["ordinal"]), chunk_type=row["chunk_type"],
        content=row["content"], title=row["doc_title"], dept=row["dept"],
        classification=int(row["classification"]),
        effective_date=str(row["effective_date"] or ""),
        metadata=metadata, distance=distance, expanded=expanded,
    )


def _visible_counts(conn: Connection, user: User) -> tuple[int, int]:
    row = conn.execute(
        f"""SELECT count(*) AS chunks, count(DISTINCT c.document_id) AS docs
              FROM chunks c
              JOIN documents d ON d.id = c.document_id
             WHERE {_ACL}""",
        (user.max_level, user.dept),
    ).fetchone()
    return int(row["chunks"]), int(row["docs"])


def _dense(conn: Connection, user: User, vector: list[float],
           settings: Settings) -> list[Hit]:
    merged: dict[int, Hit] = {}
    for level in range(0, user.max_level + 1):
        # 公开级不限部门；内部/秘密级只查本部门，命中各自的 partial HNSW 索引
        dept_sql = "" if level == 0 else " AND c.dept = %s"
        params: list = [Vector(vector), level]
        if level:
            params.append(user.dept)
        params.append(settings.dense_top)
        rows = conn.execute(
            f"""SELECT c.id AS db_id, c.document_id, c.version_id, c.ordinal,
                       c.chunk_type, c.content, c.metadata, c.dept,
                       c.classification, v.version_no,
                       d.title AS doc_title, d.effective_date,
                       c.embedding <-> %s AS distance
                  FROM chunks c
                  JOIN documents d ON d.id = c.document_id
                  JOIN doc_versions v ON v.id = c.version_id
                 WHERE c.active
                   AND d.status = 'active' AND d.deleted_at IS NULL
                   AND c.classification = %s{dept_sql}
                   AND c.embedding IS NOT NULL
                 ORDER BY distance
                 LIMIT %s""",
            params,
        ).fetchall()
        for row in rows:
            hit = _hit(row, distance=float(row["distance"]))
            merged.setdefault(hit.db_id, hit)
    return sorted(merged.values(), key=lambda h: (h.distance, h.db_id))[
        : settings.dense_top]


def _sparse(conn: Connection, user: User, query: str,
            settings: Settings) -> list[Hit]:
    tsquery = _tsquery(query)
    if tsquery is None:
        return []
    rows = conn.execute(
        f"""{_BASE}
            WHERE {_ACL}
              AND c.tsv @@ to_tsquery('simple', %s)
            ORDER BY ts_rank_cd(c.tsv, to_tsquery('simple', %s)) DESC, c.id
            LIMIT %s""",
        (user.max_level, user.dept, tsquery, tsquery, settings.sparse_top),
    ).fetchall()
    return [_hit(row) for row in rows]


def _fuse(dense: list[Hit], sparse: list[Hit], *, top: int) -> list[Hit]:
    scores: dict[int, float] = {}
    hits: dict[int, Hit] = {}
    for results in (dense, sparse):
        for rank, hit in enumerate(results, start=1):
            scores[hit.db_id] = scores.get(hit.db_id, 0.0) + 1.0 / (RRF_K + rank)
            hits.setdefault(hit.db_id, hit)
    ordered = sorted(hits.values(), key=lambda h: (-scores[h.db_id], h.db_id))
    return ordered[:top]


def _expand_table_neighbors(conn: Connection, user: User, hits: list[Hit], *,
                            limit: int = NEIGHBOR_LIMIT) -> list[Hit]:
    if limit <= 0:
        return []
    selected = {h.db_id for h in hits}
    added: list[Hit] = []
    for hit in hits:
        if len(added) >= limit:
            break
        if hit.chunk_type != "table_rows":
            continue
        table_key = hit.metadata.get("table_key")
        if not table_key:
            continue
        rows = conn.execute(
            f"""{_BASE}
                WHERE c.version_id = %s AND c.chunk_type = 'table_rows'
                  AND c.active AND d.status = 'active'
                  AND d.deleted_at IS NULL
                  AND c.classification IS NOT NULL
                  AND c.classification <= %s
                  AND (c.classification = 0 OR c.dept = %s)
                  AND c.metadata->>'table_key' = %s
                  AND c.ordinal = ANY(%s)
                  AND NOT (c.id = ANY(%s))
                ORDER BY c.ordinal""",
            (hit.version_id, user.max_level, user.dept, table_key,
             [hit.ordinal - 1, hit.ordinal + 1], list(selected)),
        ).fetchall()
        for row in rows:
            if len(added) >= limit:
                break
            neighbor = _hit(row, expanded=True)
            if neighbor.db_id in selected:
                continue
            selected.add(neighbor.db_id)
            added.append(neighbor)
    return added


def retrieve(conn: Connection, user: User, query: str, *,
             embedder: Embedder | None = None,
             settings: Settings | None = None,
             max_distance: float | None = None) -> RetrievalOutcome:
    settings = settings or get_settings()
    outcome = RetrievalOutcome(query=query)
    outcome.visible_chunks, outcome.visible_docs = _visible_counts(conn, user)
    if outcome.visible_chunks == 0:
        outcome.reason = "no_visible_docs"
        return outcome

    embedder = embedder or get_embedder(settings)
    vector = embedder.embed([query])[0]
    if len(vector) != settings.embed_dim:
        raise ModelUnavailable(
            f"查询向量维度 {len(vector)} != EMBED_DIM {settings.embed_dim}")

    dense = _dense(conn, user, vector, settings)
    sparse = _sparse(conn, user, query, settings)
    outcome.dense_hits = len(dense)
    outcome.sparse_hits = len(sparse)
    outcome.best_distance = dense[0].distance if dense else None

    fused = _fuse(dense, sparse, top=settings.fusion_top)
    outcome.hits = fused + _expand_table_neighbors(conn, user, fused)

    if outcome.best_distance is None:
        outcome.reason = "no_dense_hits"
    elif outcome.best_distance ** 2 > _threshold(max_distance):
        # RAG_MAX_DISTANCE 按 FAISS 的 similarity_search_with_score 口径校准
        # （平方 L2）；pgvector 的 `<->` 返回 L2 本身，先平方再比，否则同一个
        # 阈值在 pg 端严格得多，相关文档会被误判成低相关而拒答。
        outcome.reason = "low_relevance"
    else:
        outcome.grounded = True
    return outcome
