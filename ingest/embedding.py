from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb

from ingest import repository as repo
from ingest.chunking import chunk_document
from ingest.config import Settings, get_settings
from ingest.errors import ModelUnavailable
from ingest.ir import DocModel
from ingest.queue import enqueue

BATCH_SIZE = 32


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class OllamaEmbedder:
    model: str = "bge-m3"
    base_url: str = "http://127.0.0.1:11434"
    dim: int = 1024
    timeout: float = 120.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts},
                             ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/api/embed",
            data=payload, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelUnavailable(f"嵌入服务不可用: {exc}") from exc
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ModelUnavailable("嵌入服务返回的向量数量与输入不一致")
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.dim:
                raise ModelUnavailable(
                    f"向量维度 {len(vector) if isinstance(vector, list) else '?'} "
                    f"!= EMBED_DIM {self.dim}")
        return vectors


def get_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    return OllamaEmbedder(model=settings.embed_model,
                          base_url=settings.ollama_base_url,
                          dim=settings.embed_dim)


def embed_texts(embedder: Embedder, texts: list[str],
                batch_size: int = BATCH_SIZE) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        out = embedder.embed(batch)
        if len(out) != len(batch):
            raise ModelUnavailable("嵌入服务返回的向量数量与输入不一致")
        for vector in out:
            if len(vector) != embedder.dim:
                raise ModelUnavailable(
                    f"向量维度 {len(vector)} != EMBED_DIM {embedder.dim}")
        vectors.extend(out)
    return vectors


def _write_doc_tables(conn: Connection, version_id: str, ir: DocModel) -> None:
    keys = [table.table_key for table in ir.tables()]
    if keys:
        conn.execute(
            "DELETE FROM doc_tables WHERE version_id=%s AND NOT (table_key = ANY(%s))",
            (version_id, keys),
        )
    else:
        conn.execute("DELETE FROM doc_tables WHERE version_id=%s", (version_id,))
    for table in ir.tables():
        conn.execute(
            """INSERT INTO doc_tables (version_id, table_key, sheet, cell_range,
                   header_rows, columns, row_count, footer_rows, confidence, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (version_id, table_key) DO UPDATE SET
                   sheet=EXCLUDED.sheet, cell_range=EXCLUDED.cell_range,
                   header_rows=EXCLUDED.header_rows, columns=EXCLUDED.columns,
                   row_count=EXCLUDED.row_count, footer_rows=EXCLUDED.footer_rows,
                   confidence=EXCLUDED.confidence, source=EXCLUDED.source""",
            (version_id, table.table_key, table.sheet, table.range,
             table.header_rows, Jsonb([c.model_dump() for c in table.columns]),
             len(table.rows), table.footer_rows, table.confidence, table.source),
        )


def embed_version(conn: Connection, version_id: str, *,
                  embedder: Embedder | None = None,
                  settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    embedder = embedder or get_embedder(settings)
    # 切片与嵌入之前先校验维度：chunks.embedding 是 vector(1024)，维度不符时
    # 若走到 INSERT 会抛 psycopg 类型错误而非 ModelUnavailable；此处直接失败，
    # 尚未产生任何写入，版本保持 embedding 状态交由 worker 退避重试。
    if embedder.dim != settings.embed_dim:
        raise ModelUnavailable(
            f"嵌入维度 {embedder.dim} != EMBED_DIM {settings.embed_dim}")
    version = repo.get_version(conn, version_id)
    if version is None:
        raise ValueError(f"版本不存在: {version_id}")
    doc = repo.get_document(conn, version["document_id"])
    if doc is None or doc["deleted_at"] is not None:
        raise ValueError(f"文档不存在: {version['document_id']}")
    if not version["ir"]:
        raise ValueError(f"版本没有解析结果: {version_id}")

    ir = DocModel.model_validate(version["ir"])
    drafts = chunk_document(
        ir, doc_id=doc["id"], version_no=version["version_no"],
        title=doc["title"], dept=doc["dept"],
        classification=doc["classification"],
        source_filename=version["filename"], settings=settings,
        origin_id=doc["origin_id"],
    )
    vectors = embed_texts(embedder, [draft.content for draft in drafts])
    active = (doc["current_version_id"] == version["id"]
              and doc["status"] == "active")

    conn.execute("DELETE FROM chunks WHERE version_id=%s", (version["id"],))
    for draft, vector in zip(drafts, vectors):
        metadata = {**draft.metadata, "embed_model": embedder.model}
        conn.execute(
            """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type,
                   content, content_tokens, tsv, embedding, embed_model, dept,
                   classification, metadata, chunk_hash, active)
               VALUES (%s,%s,%s,%s,%s,%s,to_tsvector('simple', %s),%s,%s,%s,%s,%s,%s,%s)""",
            (doc["id"], version["id"], draft.ordinal, draft.chunk_type,
             draft.content, draft.content_tokens, draft.content_tokens,
             vector, embedder.model, doc["dept"], doc["classification"],
             Jsonb(metadata), draft.chunk_hash, active),
        )
    _write_doc_tables(conn, version["id"], ir)
    repo.set_version_status(conn, version["id"], "ready")

    # jobs.payload 是 jsonb：UUID 必须转字符串，且 payload->>'version_id'
    # 是 text，去重参数同样用字符串，否则比较两侧类型不一致。
    version_key = str(version["id"])
    queued = conn.execute(
        """SELECT id FROM jobs WHERE kind='activate' AND status='queued'
             AND payload->>'version_id' = %s LIMIT 1""",
        (version_key,),
    ).fetchone()
    if queued is None:
        enqueue(conn, "activate", {"version_id": version_key}, priority=40)
    else:
        conn.commit()
    return {"version_id": version["id"], "chunks": len(drafts),
            "active": active}
